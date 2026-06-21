# name:         pyall_mcp
# created:      June 2026
# by:           paul.kennedy@guardiangeomatics.com
# description:  Model Context Protocol (MCP) server exposing pyall point cloud generation tools.
#
# This server intentionally depends only on the consolidated `pyall` module for point cloud
# generation.  Concurrency is handled here with a dedicated thread pool (see _to_thread) rather than
# multiprocessing.
#
# Run with (local stdio transport, suitable for Claude Desktop / VS Code MCP clients):
#       python pyall_mcp.py
#
# Run over HTTP so a remote client (e.g. across the office network to a VM) can reach it:
#       python pyall_mcp.py --http --host 0.0.0.0 --port 8000 --root D:\surveydata
#
# When serving over HTTP, always pass one or more --root folders.  Every file system tool and
# every input/output path is confined to those roots so remote clients cannot read arbitrary
# files on the VM.  See the bottom of this file for the full command line.

import os
import io
import glob
import time
import uuid
import shutil
import base64
import asyncio
import logging
import argparse
import platform
import threading
import functools
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from mcp.server.fastmcp import FastMCP
from starlette.responses import HTMLResponse, PlainTextResponse, JSONResponse, Response, FileResponse

import pyall
import monitor

# version / authorship reported in the startup log
__version__ = "1.5.0"
__author__ = "paul.kennedy@guardiangeomatics.com"

# send all processing log output to the single shared rotating log file so the
# monitor web page (and admins) have one place to watch the whole server.
LOGPATH = pyall.setup_logging()

mcp = FastMCP("pyall")


###############################################################################
# monitor web page
#
# The monitor (live status + log) is served from the SAME HTTP server/port as the
# MCP endpoint, under /monitor, so a single port needs to be published in Docker.
# It reuses the rendering in monitor.py, pointed at the shared log folder this
# server writes to.
###############################################################################
MONITOR_BASE = "/monitor"
MONITOR_INTERVAL = 3

# (host, port) this server is bound to when served over HTTP; None for stdio.  Set in __main__.
_HTTP_ENDPOINT = None


def _monitor_watchdir():
    '''folder the monitor reads (the shared log folder this server logs to).'''
    if LOGPATH:
        return os.path.dirname(os.path.abspath(LOGPATH))
    return monitor.central_log_dir()


def _monitor_url():
    '''best-effort URL of the live monitor page (served under /monitor).  Empty over stdio.

    Prefers the address the client actually used to reach this server (so it works behind a
    reverse proxy or Docker port mapping), then falls back to the configured HTTP bind address.'''
    # 1) reflect however the current client reached us
    try:
        ctx = mcp.get_context()
        request = getattr(ctx.request_context, "request", None)
        base = getattr(request, "base_url", None)
        if base:
            return str(base).rstrip("/") + MONITOR_BASE
    except Exception:
        pass
    # 2) fall back to the configured bind address
    if _HTTP_ENDPOINT:
        host, port = _HTTP_ENDPOINT
        display = (platform.node() or "127.0.0.1") if host in ("0.0.0.0", "::") else host
        return "http://%s:%d%s" % (display, port, MONITOR_BASE)
    return ""



@mcp.custom_route(MONITOR_BASE, methods=["GET"])
@mcp.custom_route(MONITOR_BASE + "/", methods=["GET"])
async def monitor_page(request):
    html_text = await asyncio.to_thread(
        monitor.render_page, _monitor_watchdir(), MONITOR_INTERVAL, MONITOR_BASE)
    return HTMLResponse(html_text)


@mcp.custom_route(MONITOR_BASE + "/about", methods=["GET"])
async def monitor_about(request):
    html_text = await asyncio.to_thread(monitor.render_about_page, MONITOR_BASE)
    return HTMLResponse(html_text)


@mcp.custom_route(MONITOR_BASE + "/log", methods=["GET"])
async def monitor_log(request):
    text = await asyncio.to_thread(
        monitor.tail, os.path.join(_monitor_watchdir(), monitor.LOG_NAME), monitor.LOG_TAIL_LINES)
    return PlainTextResponse(text)


@mcp.custom_route(MONITOR_BASE + "/status.json", methods=["GET"])
async def monitor_status(request):
    data = await asyncio.to_thread(
        monitor.read_status, os.path.join(_monitor_watchdir(), monitor.STATUS_NAME))
    return JSONResponse(data)


@mcp.custom_route(MONITOR_BASE + "/" + monitor.LOGO_NAME, methods=["GET"])
async def monitor_logo(request):
    try:
        with open(monitor.LOGO_PATH, "rb") as fh:
            return Response(fh.read(), media_type="image/png")
    except OSError:
        return Response("not found", status_code=404, media_type="text/plain")

###############################################################################
# parallel execution
#
# Each .all processing call is CPU/IO bound and releases the GIL inside numpy /
# rasterio / file reads, so we run them on a dedicated thread pool.  This lets
# several MCP requests - including concurrent requests from different HTTP
# session ids - make progress in parallel instead of serialising behind one
# another.  The pool is sized generously relative to the CPU count.
###############################################################################
_MAXWORKERS = max(8, (os.cpu_count() or 4) * 2)
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAXWORKERS, thread_name_prefix="pyall")


async def _to_thread(func, *args, **kwargs):
    '''run a blocking pyall call on the shared worker pool so requests run in parallel.'''
    sid = _session_id()
    if sid:
        logging.debug("session %s -> %s on worker pool", sid, getattr(func, "__name__", func))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(func, *args, **kwargs))


###############################################################################
# asynchronous job registry
#
# Long-running processing (gridding, point-cloud export, batch jobs) can take far longer
# than an MCP client is willing to hold a single request open (transports commonly close
# around 30 s).  Rather than run that work inline, the long tools submit it to the worker
# pool, register it here and return a job_id immediately.  The client then polls
# get_job_status(job_id) / list_jobs() until the job is "complete" (or "error") and reads
# the output path(s) from the stored result.  The thread-local pyall progress hook updates
# each job's progress as it runs, so concurrent jobs never cross-talk.
###############################################################################
_JOBS = {}            # job_id -> serialisable status record
_JOB_FUTURES = {}     # job_id -> concurrent future (kept so it is not GC'd)
_JOBS_LOCK = threading.Lock()
_JOBS_KEEP = 200      # cap the registry so it cannot grow without bound

# tools whose work is dispatched as a background job (advertised by get_server_info)
_ASYNC_TOOLS = ("get_depth_raster", "get_backscatter_raster", "get_pointcloud", "batch_process")


def _new_job(tool, params):
    '''create and register a job record; returns the (mutable) record.'''
    jid = uuid.uuid4().hex[:12]
    now = time.time()
    rec = {
        'job_id': jid,
        'tool': tool,
        'params': params,
        'status': 'running',
        'progress': 0.0,
        'message': 'submitted',
        'submitted': now,
        'submitted_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'started': None,
        'finished': None,
        'elapsed_seconds': None,
        'result': None,
        'error': None,
    }
    with _JOBS_LOCK:
        _JOBS[jid] = rec
        if len(_JOBS) > _JOBS_KEEP:        # drop the oldest records
            for old in sorted(_JOBS.values(), key=lambda r: r['submitted'])[:len(_JOBS) - _JOBS_KEEP]:
                _JOBS.pop(old['job_id'], None)
                _JOB_FUTURES.pop(old['job_id'], None)
    return rec


def _job_set(jid, **fields):
    '''thread-safe update of a job record.'''
    with _JOBS_LOCK:
        rec = _JOBS.get(jid)
        if rec is not None:
            rec.update(fields)


def _submit_job(tool, params, func, *args, **kwargs):
    '''run *func(*args)* on the worker pool as a background job; return a compact submission dict.'''
    rec = _new_job(tool, params)
    jid = rec['job_id']
    loop = asyncio.get_running_loop()

    def hook(fraction, message=''):
        _job_set(jid, progress=round(float(fraction), 4), message=message or 'processing')

    def runner():
        _job_set(jid, status='running', started=time.time(), message='processing')
        pyall.setprogresshook(hook)
        try:
            result = func(*args, **kwargs)
            _job_set(jid, status='complete', result=result, progress=1.0,
                     message='complete', finished=time.time())
            logging.info("job %s (%s) complete", jid, tool)
            return result
        except Exception as ex:
            _job_set(jid, status='error', error=str(ex), finished=time.time(), message='failed')
            logging.error("job %s (%s) failed: %s", jid, tool, ex)
            raise
        finally:
            pyall.setprogresshook(None)
            with _JOBS_LOCK:
                r = _JOBS.get(jid)
                if r and r.get('started'):
                    r['elapsed_seconds'] = round((r.get('finished') or time.time()) - r['started'], 3)

    future = loop.run_in_executor(_EXECUTOR, runner)

    def _swallow(fut):
        try:
            fut.exception()      # retrieve so asyncio does not warn about an unretrieved exception
        except Exception:
            pass
    future.add_done_callback(_swallow)
    with _JOBS_LOCK:
        _JOB_FUTURES[jid] = future
    logging.info("job %s (%s) submitted", jid, tool)
    return {
        'job_id': jid,
        'tool': tool,
        'status': 'running',
        'submitted_at': rec['submitted_at'],
        'message': 'job submitted; poll get_job_status(job_id) until status is "complete" or "error"',
    }


def _session_id():
    '''best-effort current HTTP/MCP session id for logging; empty string when unavailable.'''
    try:
        ctx = mcp.get_context()
        for attr in ("session_id", "client_id", "request_id"):
            try:
                value = getattr(ctx, attr, None)
            except Exception:
                continue
            if value:
                return str(value)
        request = getattr(ctx.request_context, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers.get("mcp-session-id", "") or ""
    except Exception:
        pass
    return ""


###############################################################################
# per-request logging
#
# FastMCP dispatches every tool invocation through mcp.call_tool(name, arguments).
# We wrap that single entry point so the shared log file records one line when each
# MCP request arrives and another when it completes (or fails), with timing and the
# originating session id.  Wrapping the dispatcher - rather than each @mcp.tool -
# keeps the tool signatures (and FastMCP's schema/Context introspection) untouched.
#
# FastMCP captures the original handler during __init__ (_setup_handlers), so we must
# re-register the wrapper with the low-level server for it to actually intercept calls.
###############################################################################
def _install_request_logging():
    original_call_tool = mcp.call_tool

    @functools.wraps(original_call_tool)
    async def call_tool_logged(name, arguments):
        sid = _session_id()
        who = (" [session %s]" % sid) if sid else ""
        argkeys = ", ".join(sorted(arguments)) if isinstance(arguments, dict) else ""
        logging.info("MCP request -> %s(%s)%s", name, argkeys, who)
        start = time.time()
        try:
            result = await original_call_tool(name, arguments)
            logging.info("MCP request <- %s done in %.3fs%s", name, time.time() - start, who)
            return result
        except Exception as ex:
            logging.error("MCP request !! %s failed after %.3fs%s: %s",
                          name, time.time() - start, who, ex)
            raise

    mcp.call_tool = call_tool_logged
    # input validation stays off to match FastMCP's own registration (it does ad hoc conversion).
    mcp._mcp_server.call_tool(validate_input=False)(call_tool_logged)


_install_request_logging()


###############################################################################
# file system confinement
#
# When the server is exposed over HTTP the remote client effectively asks this
# process to read and write files on the host VM.  To keep that safe every path
# the client supplies is resolved and checked against an allow list of root
# folders before it is touched.  When no roots are configured (typical for local
# stdio use) paths are used as-is for backwards compatibility.
###############################################################################
_ALLOWED_ROOTS = []  # list of absolute, real paths; empty means "unrestricted"


def _set_allowed_roots(roots):
    '''configure the folders that file system tools and path arguments are confined to.'''
    global _ALLOWED_ROOTS
    resolved = []
    for root in roots or []:
        if not root:
            continue
        real = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        if not os.path.isdir(real):
            raise ValueError("Root folder does not exist or is not a directory: %s" % root)
        resolved.append(real)
    _ALLOWED_ROOTS = resolved
    return _ALLOWED_ROOTS


def _confine(path):
    '''resolve *path* and guarantee it sits inside an allowed root.

    Relative paths are resolved against the first configured root.  Returns the
    absolute, real path.  Raises ValueError on traversal outside the allow list.
    '''
    if not path:
        raise ValueError("An empty path is not allowed.")

    expanded = os.path.expanduser(path)
    if not _ALLOWED_ROOTS:
        # unrestricted (local stdio) mode - keep historical behaviour
        return os.path.abspath(expanded)

    if not os.path.isabs(expanded):
        expanded = os.path.join(_ALLOWED_ROOTS[0], expanded)
    real = os.path.realpath(os.path.abspath(expanded))

    for root in _ALLOWED_ROOTS:
        if real == root or real.startswith(root + os.sep):
            return real
    raise ValueError("Access denied: path is outside the allowed root folder(s): %s" % path)


def _safe_file(path):
    '''confine *path* and require that it is an existing file.'''
    real = _confine(path)
    if not os.path.isfile(real):
        raise ValueError("File not found: %s" % path)
    return real


def _safe_dir(path):
    '''confine *path* and require that it is an existing directory.'''
    real = _confine(path)
    if not os.path.isdir(real):
        raise ValueError("Folder not found: %s" % path)
    return real


def _safe_out(path):
    '''confine an output *path* that does not need to exist yet.'''
    return _confine(path)


###############################################################################
def _resolve_output_dir(input_file, output_dir):
    '''return an output folder, creating a timestamped one next to the input file when none is given.'''
    if output_dir:
        odir = _safe_out(output_dir)
    else:
        odir = _safe_out(os.path.join(
            os.path.dirname(os.path.abspath(input_file)),
            "all2point_%s" % (time.strftime("%Y%m%d-%H%M%S")),
        ))
    os.makedirs(odir, exist_ok=True)
    return odir


###############################################################################
def _runtime_params(epsg, output_dir, max_pings, verbose):
    '''build the runtime_params dictionary understood by the pyall functions.'''
    return {
        'epsg': str(epsg),
        'odir': output_dir,
        'debug': str(max_pings),
        'verbose': bool(verbose),
        'spherical': False,
    }


###############################################################################
# datagram record access tools
###############################################################################

###############################################################################
@mcp.tool()
async def get_file_info(input_file: str) -> dict:
    """Summarise a Kongsberg .all file (fast - it never processes the bathymetry).

    Scans only the datagram headers plus the position records and one sample of each depth/
    travel-time/runtime record, so it is quick even on large files.  Returns: datagram counts,
    file size, first/last position, survey duration, track distance, average vessel speed and
    course over ground, approximate water depth, centre frequency, swath/sector coverage angle,
    depth mode, the significant wave height / roll / pitch (4 x standard deviation of the per-record
    heave/roll/pitch), the first runtime parameter dictionary (sonar settings - depth mode, pulse
    form, filters, coverage, absorption, beam widths, etc.) and a suitable projected EPSG code.

    Args:
        input_file: Absolute path to the .all file.
    """
    input_file = _safe_file(input_file)
    return await _to_thread(pyall.getfileinfo, input_file)


###############################################################################
def _grid_to_raster_run(input_file, resolution, value, colour, epsg, output_dir,
                        max_pings, colour_min, colour_max, keep_rejected):
    '''blocking worker behind get_depth_raster / get_backscatter_raster (runs inside a job).

    Bins every sounding into a regular grid (per-cell mean) and writes a GeoTIFF.'''
    tif = pyall.depthtotif(input_file, resolution, value, colour, epsg, "", False,
                           max_pings, False, output_dir, colour_min, colour_max, keep_rejected)
    return {
        'input_file': input_file,
        'value': value,
        'colour': colour,
        'resolution': resolution,
        'colour_min': colour_min,
        'colour_max': colour_max,
        'keep_rejected': keep_rejected,
        'geotiff': tif,
    }


###############################################################################
@mcp.tool()
async def get_depth_raster(input_file: str, resolution: float = 0, colour: str = "none",
                           epsg: str = "0", output_dir: str = "", max_pings: int = -1,
                           colour_min: float | None = None, colour_max: float | None = None,
                           keep_rejected: bool = False) -> dict:
    """Grid the bathymetry from a .all file into a depth GeoTIFF (asynchronous job).

    Bins every sounding into a regular grid and writes the per-cell mean depth.  Because gridding a
    whole file can take longer than the MCP request timeout, this submits the work as a background
    job and returns immediately with a ``job_id``.  Poll ``get_job_status(job_id)`` until its status
    is ``complete``; the job ``result`` then carries the output ``geotiff`` path for ``download_file``.

    Args:
        input_file: Absolute path to the .all file.
        resolution: Grid cell size in metres. 0 (default) auto-computes from the beam spacing, snapped to a sensible interval (0.5, 1, 2, 5, 10, ...).
        colour: Rendering: "none" (float, default), "jeca" (colour ramp) or "grey" (greyscale).
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write the tif to. Empty writes next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        colour_min: Minimum depth to stretch the palette across. None uses the full data range.
        colour_max: Maximum depth to stretch the palette across. None uses the full data range.
        keep_rejected: When False (default) rejected soundings are excluded from the grid. True grids every sounding.
    """
    input_file = _safe_file(input_file)
    odir = ""
    if output_dir:
        odir = _safe_out(output_dir)
        os.makedirs(odir, exist_ok=True)
    return _submit_job(
        "get_depth_raster",
        {'input_file': input_file, 'value': 'depth', 'colour': colour, 'resolution': resolution, 'max_pings': max_pings},
        _grid_to_raster_run, input_file, resolution, "depth", colour, epsg, odir,
        max_pings, colour_min, colour_max, keep_rejected)


###############################################################################
@mcp.tool()
async def get_backscatter_raster(input_file: str, resolution: float = 0, colour: str = "grey",
                                 epsg: str = "0", output_dir: str = "", max_pings: int = -1,
                                 colour_min: float | None = None, colour_max: float | None = None,
                                 keep_rejected: bool = False) -> dict:
    """Grid the seabed backscatter (reflectivity) from a .all file into a GeoTIFF (asynchronous job).

    Bins every sounding into a regular grid and writes the per-cell mean reflectivity.  This submits
    the work as a background job and returns a ``job_id`` immediately; poll ``get_job_status(job_id)``
    until ``complete`` and read the output ``geotiff`` path from the job ``result``.

    Args:
        input_file: Absolute path to the .all file.
        resolution: Grid cell size in metres. 0 (default) auto-computes from the beam spacing, snapped to a sensible interval (0.5, 1, 2, 5, 10, ...).
        colour: Rendering: "grey" (greyscale, default), "jeca" (colour ramp) or "none" (float).
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write the tif to. Empty writes next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        colour_min: Minimum reflectivity to stretch the palette across. None uses the full data range.
        colour_max: Maximum reflectivity to stretch the palette across. None uses the full data range.
        keep_rejected: When False (default) rejected soundings are excluded from the grid. True grids every sounding.
    """
    input_file = _safe_file(input_file)
    odir = ""
    if output_dir:
        odir = _safe_out(output_dir)
        os.makedirs(odir, exist_ok=True)
    return _submit_job(
        "get_backscatter_raster",
        {'input_file': input_file, 'value': 'reflectivity', 'colour': colour, 'resolution': resolution, 'max_pings': max_pings},
        _grid_to_raster_run, input_file, resolution, "reflectivity", colour, epsg, odir,
        max_pings, colour_min, colour_max, keep_rejected)



###############################################################################
def _make_pointcloud_csv(input_file, params):
    '''load a .all file into a point cloud and write it to a CSV.  returns (csv_path, point_count).'''
    pointcloud = pyall.loaddata(input_file, params)
    count = len(pointcloud.xarr)
    csv = os.path.join(params['odir'], os.path.basename(input_file) + "_R.txt")
    if count == 0:
        return None, 0
    pyall._savexyzcsv(csv, pointcloud.xarr, pointcloud.yarr, pointcloud.zarr,
                      pointcloud.qarr, pointcloud.rarr)
    return csv, count


###############################################################################
def _pointcloud_run(input_file, params):
    '''blocking worker behind get_pointcloud (runs inside a job).'''
    if str(params['epsg']) == '0':
        params['epsg'] = str(pyall.getsuitableepsg(input_file))
    csv, count = _make_pointcloud_csv(input_file, params)
    return {
        'input_file': input_file,
        'epsg': params['epsg'],
        'output_dir': params['odir'],
        'pointcloud_csv': csv,
        'point_count': count,
    }


###############################################################################
@mcp.tool()
async def get_pointcloud(input_file: str, epsg: str = "0", output_dir: str = "",
                         max_pings: int = -1, verbose: bool = False) -> dict:
    """Read a .all file and export the bathymetric point cloud as a CSV (asynchronous job).

    The CSV columns are: east, north, depth, quality, reflectivity.  Exporting a whole file can take
    longer than the MCP request timeout, so this submits a background job and returns a ``job_id``
    immediately.  Poll ``get_job_status(job_id)`` until ``complete``; the job ``result`` carries the
    ``pointcloud_csv`` path (for ``download_file``) and the ``point_count``.

    Args:
        input_file: Absolute path to the .all file.
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write outputs to. Empty creates a timestamped folder next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        verbose: Enable verbose logging.
    """
    input_file = _safe_file(input_file)
    odir = _resolve_output_dir(input_file, output_dir)
    params = _runtime_params(epsg, odir, max_pings, verbose)
    return _submit_job(
        "get_pointcloud",
        {'input_file': input_file, 'output_dir': odir, 'max_pings': max_pings},
        _pointcloud_run, input_file, params)



###############################################################################
def _batch_run(matches, operation, odir, epsg, max_pings, max_concurrency, resolution,
               value, colour, colour_min, colour_max, keep_rejected):
    '''blocking worker behind batch_process (runs inside a job).  Processes the files with an
    internal thread pool and reports overall progress (files completed) via the job hook.'''
    def one(filename):
        try:
            if operation == "grid":
                tif = pyall.depthtotif(filename, resolution, value, colour, epsg, "", False,
                                       max_pings, False, odir, colour_min, colour_max, keep_rejected)
                return {'input_file': filename, 'geotiff': tif, 'error': None}
            params = _runtime_params(epsg, odir, max_pings, False)
            csv, count = _make_pointcloud_csv(filename, params)
            return {'input_file': filename, 'pointcloud_csv': csv, 'point_count': count, 'error': None}
        except Exception as ex:                      # report per-file failures without aborting the batch
            return {'input_file': filename, 'error': str(ex)}

    results = []
    total = len(matches)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(max_concurrency))) as pool:
        futures = [pool.submit(one, f) for f in matches]
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            pyall._emitprogress(done / total, "processed %d/%d files" % (done, total))

    succeeded = sum(1 for r in results if r.get('error') is None)
    return {
        'operation': operation,
        'output_dir': odir,
        'count': len(results),
        'succeeded': succeeded,
        'failed': len(results) - succeeded,
        'processed': results,
    }


###############################################################################
@mcp.tool()
async def batch_process(input_folder: str, operation: str = "pointcloud", epsg: str = "0",
                        output_dir: str = "", max_pings: int = -1, recursive: bool = False,
                        max_concurrency: int = 4, resolution: float = 0, value: str = "depth",
                        colour: str = "none", colour_min: float | None = None,
                        colour_max: float | None = None, keep_rejected: bool = False) -> dict:
    """Process every .all file in a folder in parallel (asynchronous job).

    Files are processed concurrently using a thread pool, bounded by max_concurrency.  A whole survey
    folder can take a long time, so this submits a background job and returns a ``job_id`` immediately.
    Poll ``get_job_status(job_id)``; the job ``progress`` advances as files finish and the ``result``
    lists every file's output path (and any per-file error) once ``complete``.

    Args:
        input_folder: Absolute path to a folder containing .all files.
        operation: "pointcloud" (default) writes a point cloud CSV per file;
                   "grid" writes a gridded GeoTIFF per file (see resolution/value/colour).
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS per file.
        output_dir: Folder to write outputs to. Empty creates a timestamped folder inside the input folder.
        max_pings: Number of pings to process per file. -1 (default) processes all pings.
        recursive: Search sub-folders for .all files.
        max_concurrency: Maximum number of files to process at once.
        resolution: ("grid") cell size in metres. 0 auto-computes a sensible interval per file.
        value: ("grid") quantity to grid: "depth" (default) or "reflectivity".
        colour: ("grid") rendering: "none" (float), "jeca" (colour ramp) or "grey" (greyscale).
        colour_min: ("grid") minimum value to stretch the palette across. None uses each file's full range.
        colour_max: ("grid") maximum value to stretch the palette across. None uses each file's full range.
    """
    import fileutils

    if operation not in ("pointcloud", "grid"):
        raise ValueError("operation must be 'pointcloud' or 'grid', got: %s" % operation)
    input_folder = _safe_dir(input_folder)
    matches = fileutils.findFiles2(bool(recursive), input_folder, "*.all")
    if len(matches) == 0:
        return {'input_folder': input_folder, 'operation': operation, 'count': 0,
                'message': 'no .all files found'}

    if output_dir:
        odir = _safe_out(output_dir)
    else:
        odir = os.path.join(input_folder, "all2point_%s" % (time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(odir, exist_ok=True)

    return _submit_job(
        "batch_process",
        {'input_folder': input_folder, 'operation': operation, 'files': len(matches), 'output_dir': odir},
        _batch_run, matches, operation, odir, epsg, max_pings, max_concurrency,
        resolution, value, colour, colour_min, colour_max, keep_rejected)


###############################################################################
@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """Check the status of a background job submitted by a long-running tool.

    Long tools (get_depth_raster, get_backscatter_raster, get_pointcloud, batch_process) return a
    ``job_id`` immediately and run in the background.  Call this with that id to see whether the job
    is ``running``, ``complete`` or ``error``.  When ``complete`` the ``result`` field holds exactly
    what the tool computed (e.g. the output ``geotiff`` / ``pointcloud_csv`` path) so you can pass it
    straight to ``download_file``.  When ``error`` the ``error`` field explains why.

    Args:
        job_id: The id returned when the long-running tool was called.
    """
    with _JOBS_LOCK:
        rec = _JOBS.get(job_id)
        rec = dict(rec) if rec is not None else None
    if rec is None:
        raise ValueError("Unknown job_id: %s (it may have expired from the registry)." % job_id)
    return rec


###############################################################################
@mcp.tool()
async def list_jobs(status: str = "", max_results: int = 50) -> dict:
    """List recent background jobs (most recent first), optionally filtered by status.

    Use this to see what is running or has recently finished without having to remember each job_id.
    Returns compact summaries (no large result payloads); call get_job_status(job_id) for the full
    record including the output path(s).

    Args:
        status: Optional filter - "running", "complete" or "error". Empty returns all.
        max_results: Maximum number of jobs to return (default 50).
    """
    with _JOBS_LOCK:
        recs = [dict(r) for r in _JOBS.values()]
    if status:
        recs = [r for r in recs if r['status'] == status]
    recs.sort(key=lambda r: r['submitted'], reverse=True)
    total = len(recs)
    recs = recs[:max(1, int(max_results))]
    keys = ('job_id', 'tool', 'status', 'progress', 'message', 'submitted_at', 'elapsed_seconds')
    summaries = [{k: r.get(k) for k in keys} for r in recs]
    return {'count': total, 'returned': len(summaries), 'jobs': summaries}



###############################################################################
# file system access tools
#
# These let a remote MCP client discover and read files on the host VM.  Every
# path is confined to the configured --root folder(s); without a configured root
# the tools operate relative to the process working directory (local use).
###############################################################################
@mcp.tool()
async def get_server_info() -> dict:
    """Report what this server can see.

    Returns the server version, the live monitor web page location (path + URL), the shared
    status_file / log_file paths, the upload/download HTTP endpoints, the list of async (job-based)
    tools, the configured root folder(s) that file paths are confined to and the list of available
    tools.  Use this first when connecting over HTTP to learn the server version, where to watch
    progress, which tools run as background jobs, and which folders you can browse for .all files.
    """
    toolnames = sorted(t.name for t in await mcp.list_tools())
    return {
        'server': 'pyall',
        'version': __version__,
        'author': __author__,
        'monitor_path': MONITOR_BASE,
        'monitor_url': _monitor_url(),
        'upload_endpoint': 'PUT/POST /upload/<filename.all>[?output_dir=&overwrite=true]',
        'download_endpoint': 'GET /download/<root-relative-path>',
        'status_file': os.path.join(pyall.logdirectory(), pyall.STATUSFILENAME),
        'log_file': LOGPATH,
        'async_tools': list(_ASYNC_TOOLS),
        'job_tools': ['get_job_status', 'list_jobs'],
        'async_note': ('long-running tools (%s) return a job_id immediately and run in the '
                       'background; poll get_job_status(job_id) instead of waiting on the call.'
                       % ', '.join(_ASYNC_TOOLS)),
        'allowed_roots': list(_ALLOWED_ROOTS),
        'restricted': bool(_ALLOWED_ROOTS),
        'working_directory': os.getcwd(),
        'tools': toolnames,
    }


###############################################################################
@mcp.tool()
async def list_directory(path: str = "") -> dict:
    """List the files and sub-folders in a directory on the host VM.

    Use this to browse for .all files (and the GeoTIFF / CSV outputs the
    processing tools create) before calling the other tools.

    Args:
        path: Folder to list. Empty lists the first configured root folder (or the
              working directory when the server is unrestricted).
    """
    if not path:
        target = _ALLOWED_ROOTS[0] if _ALLOWED_ROOTS else os.getcwd()
    else:
        target = _safe_dir(path)

    def _list():
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    stat = entry.stat()
                    size = stat.st_size
                    modified = stat.st_mtime
                except OSError:
                    size = None
                    modified = None
                entries.append({
                    'name': entry.name,
                    'path': os.path.join(target, entry.name),
                    'type': 'dir' if entry.is_dir() else 'file',
                    'size': size,
                    'modified': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified)) if modified else None,
                })
        entries.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
        return entries

    entries = await _to_thread(_list)
    return {'path': target, 'count': len(entries), 'entries': entries}


###############################################################################
@mcp.tool()
async def find_files(folder: str = "", pattern: str = "*.all", recursive: bool = True,
                     max_results: int = 1000) -> dict:
    """Search a folder on the host VM for files matching a glob pattern.

    Args:
        folder: Folder to search. Empty searches the first configured root (or the working directory).
        pattern: Glob pattern, e.g. "*.all" (default), "*.tif" or "*_R.txt".
        recursive: Search sub-folders as well (default True).
        max_results: Maximum number of paths to return (default 1000).
    """
    if not folder:
        base = _ALLOWED_ROOTS[0] if _ALLOWED_ROOTS else os.getcwd()
    else:
        base = _safe_dir(folder)

    def _find():
        if recursive:
            globbed = glob.glob(os.path.join(base, '**', pattern), recursive=True)
        else:
            globbed = glob.glob(os.path.join(base, pattern))
        files = sorted(p for p in globbed if os.path.isfile(p))
        return files

    files = await _to_thread(_find)
    total = len(files)
    truncated = total > max_results
    return {
        'folder': base,
        'pattern': pattern,
        'recursive': bool(recursive),
        'count': total,
        'truncated': truncated,
        'files': files[:max_results],
    }


###############################################################################
@mcp.tool()
async def stat_path(path: str) -> dict:
    """Return metadata about a single file or folder on the host VM.

    Args:
        path: Absolute (or root-relative) path to a file or folder.
    """
    real = _confine(path)
    if not os.path.exists(real):
        raise ValueError("Path not found: %s" % path)
    stat = os.stat(real)
    return {
        'path': real,
        'type': 'dir' if os.path.isdir(real) else 'file',
        'size': stat.st_size,
        'modified': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
    }


###############################################################################
@mcp.tool()
async def read_text_file(path: str, max_bytes: int = 65536, offset: int = 0) -> dict:
    """Read a slice of a text file on the host VM (e.g. a point cloud CSV or a log).

    Designed for the text outputs this server produces - point cloud CSVs
    (``*_R.txt``) and log files.  Binary files such as GeoTIFFs are not returned.

    Args:
        path: Absolute (or root-relative) path to the text file.
        max_bytes: Maximum number of bytes to read (default 65536, capped at 1 MB).
        offset: Byte offset to start reading from (default 0) for paging through large files.
    """
    real = _safe_file(path)
    max_bytes = max(1, min(int(max_bytes), 1024 * 1024))
    offset = max(0, int(offset))

    def _read():
        size = os.path.getsize(real)
        with open(real, 'rb') as fh:
            fh.seek(offset)
            raw = fh.read(max_bytes)
        return size, raw

    size, raw = await _to_thread(_read)
    if b'\x00' in raw:
        raise ValueError("Refusing to read what looks like a binary file: %s" % path)
    text = raw.decode('utf-8', errors='replace')
    end = offset + len(raw)
    return {
        'path': real,
        'size': size,
        'offset': offset,
        'bytes_read': len(raw),
        'eof': end >= size,
        'content': text,
    }


###############################################################################
# file transfer tools (download processed outputs)
#
# Uploads are handled by the streaming HTTP /upload endpoint below rather than an
# MCP tool, so large .all files transfer in one request without base64 chunking.
###############################################################################
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024     # 64 MB returned per download call


###############################################################################
@mcp.tool()
async def download_file(path: str, offset: int = 0, max_bytes: int = 0) -> dict:
    """Download a processed output file (GeoTIFF, XYZ/CSV, log, ...) as base64 bytes.

    Works for binary files such as GeoTIFFs (``.tif``) as well as text files
    (``*_R.txt`` point clouds, logs).  Large files can be fetched in chunks: pass
    ``offset`` together with ``max_bytes`` and keep requesting until ``eof`` is true,
    using the returned ``next_offset`` each time.

    Args:
        path: Absolute (or root-relative) path to the file to download.
        offset: Byte offset to start reading from (default 0).
        max_bytes: Maximum bytes to return this call. 0 (default) returns up to the
                   server limit (64 MB); for larger files the response reports
                   eof=false and next_offset so you can page through it.
    """
    real = _safe_file(path)
    offset = max(0, int(offset))
    cap = MAX_DOWNLOAD_BYTES if max_bytes <= 0 else min(int(max_bytes), MAX_DOWNLOAD_BYTES)

    def _read():
        size = os.path.getsize(real)
        with open(real, 'rb') as fh:
            fh.seek(offset)
            raw = fh.read(cap)
        return size, raw

    size, raw = await _to_thread(_read)
    end = offset + len(raw)
    mime, _ = mimetypes.guess_type(real)
    return {
        'path': real,
        'filename': os.path.basename(real),
        'mime_type': mime or 'application/octet-stream',
        'total_size': size,
        'offset': offset,
        'bytes_read': len(raw),
        'eof': end >= size,
        'next_offset': end if end < size else None,
        'encoding': 'base64',
        'content_base64': base64.b64encode(raw).decode('ascii'),
    }


###############################################################################
@mcp.tool()
async def copy_file(source_path: str, output_dir: str = "", new_name: str = "",
                    overwrite: bool = False) -> dict:
    """Copy a file that is already on the server into an output folder (no upload needed).

    Use this to bring a large .all file the server can already see (e.g. on a mounted survey drive
    that is one of the allowed roots) into your working folder without base64-uploading it.  The copy
    happens entirely on the server, so it is fast even for multi-GB files.  Both the source and the
    destination stay confined to the configured --root folder(s).

    Args:
        source_path: Path to an existing file on the server (within an allowed root).
        output_dir: Folder to copy into. Empty uses the first allowed root.
        new_name: Optional new file name; empty keeps the source's name.
        overwrite: Allow replacing an existing destination file (default False).
    """
    src = _safe_file(source_path)
    name = os.path.basename(new_name) if new_name else os.path.basename(src)
    if not name:
        raise ValueError("A destination file name is required.")
    destdir = _safe_out(output_dir) if output_dir else (_ALLOWED_ROOTS[0] if _ALLOWED_ROOTS else os.getcwd())
    dest = _safe_out(os.path.join(destdir, name))
    if os.path.realpath(dest) == os.path.realpath(src):
        raise ValueError("Source and destination are the same file.")
    if os.path.exists(dest) and not overwrite:
        raise ValueError("Destination already exists: %s (set overwrite=true)." % dest)

    def _copy():
        parent = os.path.dirname(dest)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dest)
        return os.path.getsize(dest)

    size = await _to_thread(_copy)
    return {'source': src, 'path': dest, 'filename': name, 'size': size}


###############################################################################
# streaming HTTP file transfer endpoints
#
# The download_file tool above carries file bytes as base64 inside JSON-RPC messages
# - fine for small files, but large files then have to be paged with offset/max_bytes.
# These plain-HTTP routes stream the request/response body straight to/from disk in
# constant memory, so a client can upload or download an arbitrarily large file in a
# SINGLE request with no client-side chunking.  They share the host/port with the MCP
# and monitor endpoints, and every path stays confined to the configured --root
# folder(s) exactly like the tools do.
#
#   upload :  curl -T survey.all "http://host:8000/upload/survey.all?overwrite=true"
#   download: curl -OJ           "http://host:8000/download/outputs/result.tif"
###############################################################################
def _truthy(value):
    return str(value).lower() in ('1', 'true', 'yes', 'on')


@mcp.custom_route("/upload/{filename:path}", methods=["PUT", "POST"])
async def upload_route(request):
    '''stream an uploaded .all file body straight to disk (no base64, no chunking).

    The raw file bytes are the request body.  Optional query params: output_dir (folder
    under an allowed root) and overwrite=true (replace an existing file).'''
    name = os.path.basename(request.path_params.get("filename", "") or "")
    if not name:
        return JSONResponse({'error': 'a filename is required'}, status_code=400)
    if not name.lower().endswith('.all'):
        return JSONResponse({'error': 'only Kongsberg .all files may be uploaded (must end in .all)'},
                            status_code=400)

    out = request.query_params.get("output_dir", "")
    overwrite = _truthy(request.query_params.get("overwrite", "false"))
    try:
        destdir = _safe_out(out) if out else (_ALLOWED_ROOTS[0] if _ALLOWED_ROOTS else os.getcwd())
        dest = _safe_out(os.path.join(destdir, name))
    except ValueError as ex:
        return JSONResponse({'error': str(ex)}, status_code=403)

    if os.path.exists(dest) and not overwrite:
        return JSONResponse({'error': 'file already exists; pass ?overwrite=true', 'path': dest},
                            status_code=409)
    parent = os.path.dirname(dest)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    logging.info("HTTP upload -> %s", dest)
    start = time.time()
    total = 0
    try:
        fh = await asyncio.to_thread(open, dest, 'wb')
        try:
            async for chunk in request.stream():
                if chunk:
                    await asyncio.to_thread(fh.write, chunk)
                    total += len(chunk)
        finally:
            await asyncio.to_thread(fh.close)
    except Exception as ex:
        logging.error("HTTP upload !! %s failed after %.3fs: %s", dest, time.time() - start, ex)
        return JSONResponse({'error': str(ex), 'path': dest, 'bytes_written': total}, status_code=500)

    logging.info("HTTP upload <- %s (%d bytes in %.3fs)", dest, total, time.time() - start)
    return JSONResponse({'path': dest, 'filename': name, 'total_size': total})


@mcp.custom_route("/download/{path:path}", methods=["GET"])
async def download_route(request):
    '''stream a file from disk to the client (no base64, no chunking).

    GET the file by its root-relative (or absolute, allowed) path.  Starlette's
    FileResponse streams from disk and honours HTTP Range requests for resumable fetches.'''
    rel = request.path_params.get("path", "") or ""
    try:
        real = _safe_file(rel)
    except ValueError as ex:
        status = 404 if 'not found' in str(ex).lower() else 403
        return JSONResponse({'error': str(ex)}, status_code=status)
    mime, _ = mimetypes.guess_type(real)
    logging.info("HTTP download -> %s", real)
    return FileResponse(real, media_type=mime or 'application/octet-stream',
                        filename=os.path.basename(real))


###############################################################################
# datagram record access tools
###############################################################################

def _require_file(input_file):
    return _safe_file(input_file)


def _limit(records, max_records):
    '''return (possibly truncated list, total count, truncated flag).'''
    total = len(records)
    if max_records and max_records > 0 and total > max_records:
        return records[:max_records], total, True
    return records, total, False


###############################################################################
@mcp.tool()
async def get_positions(input_file: str, max_records: int = 5000) -> dict:
    """Return position (P) records: latitude, longitude, quality, speed, course, heading and timestamp.

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadpositions, input_file)
    data, total, truncated = _limit(recs, max_records)
    return {'input_file': input_file, 'count': total, 'truncated': truncated, 'positions': data}


###############################################################################
@mcp.tool()
async def get_attitude(input_file: str, max_records: int = 5000) -> dict:
    """Return attitude (A) observations as rows of [timestamp, roll, pitch, heave, heading].

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of rows to return (0 = all).
    """
    _require_file(input_file)
    arr = await _to_thread(pyall.loadattitude, input_file)
    total = int(arr.shape[0])
    rows = arr.tolist()
    truncated = False
    if max_records and max_records > 0 and total > max_records:
        rows = rows[:max_records]
        truncated = True
    return {
        'input_file': input_file,
        'count': total,
        'truncated': truncated,
        'columns': ['timestamp', 'roll', 'pitch', 'heave', 'heading'],
        'attitude': rows,
    }


###############################################################################
@mcp.tool()
async def get_significantwaveheight(input_file: str) -> dict:
    """Estimate the significant wave height, roll and pitch from the attitude (A) data.

    Uses the standard 4 x standard-deviation estimator: significant value = 4 * sigma, applied to the
    vessel heave, roll and pitch time series in the attitude datagrams.  A fast reader takes a single
    roll/pitch/heave sample from each attitude record (reading only 32 bytes per record, all three in
    one pass), so it stays quick even on large files.  Returns the significant wave height (metres),
    significant roll and pitch (degrees) and the sample count.

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    result = await _to_thread(pyall.significantattitude, input_file)
    result['input_file'] = input_file
    return result


###############################################################################
@mcp.tool()
async def get_network_attitude(input_file: str, max_records: int = 5000) -> dict:
    """Return network attitude (n) observations as rows of [timestamp, roll, pitch, heave, heading].

    This is the attitude received over the network interface (datagram 'n'), separate from the
    'A' attitude records.

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of rows to return (0 = all).
    """
    _require_file(input_file)
    arr = await _to_thread(pyall.loadnetworkattitude, input_file)
    total = int(arr.shape[0])
    rows = arr.tolist()
    truncated = False
    if max_records and max_records > 0 and total > max_records:
        rows = rows[:max_records]
        truncated = True
    return {
        'input_file': input_file,
        'count': total,
        'truncated': truncated,
        'columns': ['timestamp', 'roll', 'pitch', 'heave', 'heading'],
        'networkattitude': rows,
    }


###############################################################################
@mcp.tool()
async def get_clock(input_file: str, max_records: int = 5000) -> dict:
    """Return clock (C) records for analysing clock stability (PC time vs external time and PPS).

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadclock, input_file)
    data, total, truncated = _limit(recs, max_records)
    return {'input_file': input_file, 'count': total, 'truncated': truncated, 'clock': data}


###############################################################################
@mcp.tool()
async def get_height(input_file: str, max_records: int = 5000) -> dict:
    """Return height (h) records (height and height type).

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadheight, input_file)
    data, total, truncated = _limit(recs, max_records)
    return {'input_file': input_file, 'count': total, 'truncated': truncated, 'height': data}


###############################################################################
@mcp.tool()
async def get_sound_velocity_profiles(input_file: str) -> dict:
    """Return sound velocity profile (U) datagrams, each with depth and sound speed arrays.

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadsoundvelocityprofiles, input_file)
    return {'input_file': input_file, 'count': len(recs), 'profiles': recs}


###############################################################################
@mcp.tool()
async def get_surface_sound_speed(input_file: str) -> dict:
    """Return surface sound speed (G) datagrams, including mean/min/max sound speed in m/s.

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadsurfacesoundspeed, input_file)
    return {'input_file': input_file, 'count': len(recs), 'surfacesoundspeed': recs}


###############################################################################
@mcp.tool()
async def get_runtime_parameters(input_file: str) -> dict:
    """Return runtime parameter (R) records: decoded sonar settings (depth mode, filters, coverage, etc.).

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadruntimeparameters, input_file)
    return {'input_file': input_file, 'count': len(recs), 'runtimeparameters': recs}


###############################################################################
@mcp.tool()
async def get_travel_time(input_file: str, max_records: int = 200) -> dict:
    """Return raw range and beam angle (N) records with per-beam pointing angle, two way travel time,
    reflectivity and quality.  These records are large, so max_records defaults to 200.

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadtraveltime, input_file, max_records)
    return {'input_file': input_file, 'count': len(recs), 'traveltime': recs}


###############################################################################
@mcp.tool()
async def get_installation_parameters(input_file: str) -> dict:
    """Return the installation (I) datagram parameters (sensor offsets, serial numbers, etc.).

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    info = await _to_thread(pyall.loadinstallationparameters, input_file)
    return {'input_file': input_file, 'installation': info}


###############################################################################
@mcp.tool()
async def get_depth(input_file: str, max_pings: int = 50, max_points: int = 20000,
                    format: str = "csv") -> dict:
    """Return per-beam soundings from the X (and D) depth datagrams: pingtimestamp, depth, acrosstrack,
    alongtrack, reflectivity and quality.  These are large, so max_pings/max_points bound the response.

    By default the points are returned as a single compact CSV string (one row per beam) so the reply
    stays small; pass format="columns" to instead get one list per column (much larger when the client
    pretty-prints it).  For summary statistics without any raw points use get_depth_stats.

    Args:
        input_file: Absolute path to the .all file.
        max_pings: Maximum number of pings to read (-1 = all).
        max_points: Maximum number of beam points to return (0 = all read).
        format: "csv" (default, compact single string) or "columns" (one list per field).
    """
    _require_file(input_file)
    d = await _to_thread(pyall.loaddepth, input_file, max_pings)
    total = int(d['depth'].shape[0])
    n = total
    truncated = False
    if max_points and max_points > 0 and total > max_points:
        n = max_points
        truncated = True

    columns = ['pingtimestamp', 'depth', 'acrosstrack', 'alongtrack', 'reflectivity', 'quality']
    base = {
        'input_file': input_file,
        'count': total,
        'returned': n,
        'truncated': truncated,
        'columns': columns,
    }
    if format == "columns":
        base.update({
            'format': 'columns',
            'pingtimestamp': d['pingtimestamp'][:n].tolist(),
            'depth': d['depth'][:n].tolist(),
            'acrosstrack': d['acrosstrack'][:n].tolist(),
            'alongtrack': d['alongtrack'][:n].tolist(),
            'reflectivity': d['reflectivity'][:n].tolist(),
            'quality': d['quality'][:n].tolist(),
        })
        return base

    # default: compact CSV string (a single value in the JSON, so it never explodes line-per-point)
    def _csv():
        arr = np.column_stack([
            d['pingtimestamp'][:n], d['depth'][:n], d['acrosstrack'][:n],
            d['alongtrack'][:n], d['reflectivity'][:n], d['quality'][:n],
        ])
        buf = io.StringIO()
        np.savetxt(buf, arr, fmt='%.3f', delimiter=',')
        return buf.getvalue()

    base['format'] = 'csv'
    base['csv'] = await _to_thread(_csv)
    return base


###############################################################################
@mcp.tool()
async def get_depth_stats(input_file: str, max_pings: int = -1, bins: int = 20) -> dict:
    """Summary statistics of the per-beam soundings without returning any raw points.

    Reads the X/D depth datagrams and reports count / min / max / mean / std / 5th-50th-95th
    percentiles for depth, across-track distance and reflectivity, plus a depth histogram.  Use this
    instead of get_depth when you only need the distribution (it returns a tiny, fixed-size reply no
    matter how many soundings the file holds).

    Args:
        input_file: Absolute path to the .all file.
        max_pings: Maximum number of pings to read (-1 = all, default).
        bins: Number of histogram bins for the depth distribution (default 20).
    """
    _require_file(input_file)
    d = await _to_thread(pyall.loaddepth, input_file, max_pings)

    def _summary(a):
        a = a[np.isfinite(a)]
        if a.size == 0:
            return None
        return {
            'count': int(a.size),
            'min': float(a.min()),
            'max': float(a.max()),
            'mean': float(a.mean()),
            'std': float(a.std()),
            'p05': float(np.percentile(a, 5)),
            'median': float(np.percentile(a, 50)),
            'p95': float(np.percentile(a, 95)),
        }

    def _compute():
        depth = np.asarray(d['depth'], dtype=float)
        finite = depth[np.isfinite(depth)]
        histogram = None
        if finite.size:
            counts, edges = np.histogram(finite, bins=max(1, int(bins)))
            histogram = {'bin_edges': [round(float(e), 3) for e in edges],
                         'counts': [int(c) for c in counts]}
        return {
            'depth': _summary(depth),
            'acrosstrack': _summary(np.asarray(d['acrosstrack'], dtype=float)),
            'reflectivity': _summary(np.asarray(d['reflectivity'], dtype=float)),
            'depth_histogram': histogram,
        }

    stats = await _to_thread(_compute)
    return {
        'input_file': input_file,
        'soundingcount': int(d['depth'].shape[0]),
        **stats,
    }



###############################################################################
@mcp.tool()
async def get_seabed_image(input_file: str, max_pings: int = 50, max_samples: int = 50000) -> dict:
    """Return seabed image (Y) backscatter samples (0.1 dB) with per-ping sample counts.
    These are large, so max_pings/max_samples bound the response.

    Args:
        input_file: Absolute path to the .all file.
        max_pings: Maximum number of pings to read (-1 = all).
        max_samples: Maximum number of samples to return (0 = all read).
    """
    _require_file(input_file)
    d = await _to_thread(pyall.loadseabedimage, input_file, max_pings)
    total = int(d['samples'].shape[0])
    n = total
    truncated = False
    if max_samples and max_samples > 0 and total > max_samples:
        n = max_samples
        truncated = True
    return {
        'input_file': input_file,
        'pingcount': int(d['pingtimestamp'].shape[0]),
        'samplecount': total,
        'returnedsamples': n,
        'truncated': truncated,
        'pingtimestamp': d['pingtimestamp'].tolist(),
        'numsamples': d['numsamples'].tolist(),
        'samples': d['samples'][:n].tolist(),
    }


###############################################################################
@mcp.tool()
async def get_pu_status(input_file: str, max_records: int = 5000) -> dict:
    """Return PU status (1) records: sensor input health and last received sensor values
    (ping rate, sound speed, heading, roll, pitch, depth, CPU temperature, etc.).

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await _to_thread(pyall.loadpustatus, input_file)
    data, total, truncated = _limit(recs, max_records)
    return {'input_file': input_file, 'count': total, 'truncated': truncated, 'pustatus': data}


###############################################################################
def _parse_args(argv):
    '''parse command line arguments, falling back to PYALL_MCP_* environment variables.'''
    parser = argparse.ArgumentParser(
        description="pyall MCP server - exposes Kongsberg .all processing tools to MCP clients.",
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http", "streamable-http", "sse"],
        default=os.environ.get("PYALL_MCP_TRANSPORT", "stdio"),
        help="Transport to use. 'stdio' (default) for a local client; 'http' (alias of "
             "'streamable-http') or 'sse' to serve over the network.",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Shorthand for --transport http (serve over HTTP).",
    )
    parser.add_argument(
        "--host", default=os.environ.get("PYALL_MCP_HOST", "127.0.0.1"),
        help="Host/interface to bind when serving over HTTP. Use 0.0.0.0 to accept "
             "connections from other machines on the office network (default 127.0.0.1).",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PYALL_MCP_PORT", "8000")),
        help="TCP port to listen on when serving over HTTP (default 8000).",
    )
    parser.add_argument(
        "--root", action="append", default=None,
        help="Folder the file system tools and all path arguments are confined to. May be "
             "repeated. Strongly recommended when serving over HTTP. Defaults to the "
             "PYALL_MCP_ROOT environment variable (os.pathsep separated) if set.",
    )
    args = parser.parse_args(argv)

    if args.http and args.transport == "stdio":
        args.transport = "http"
    if args.transport == "http":
        args.transport = "streamable-http"

    if args.root is None:
        env_roots = os.environ.get("PYALL_MCP_ROOT", "")
        args.root = [r for r in env_roots.split(os.pathsep) if r] if env_roots else []

    return args


###############################################################################
if __name__ == "__main__":
    import sys

    args = _parse_args(sys.argv[1:])
    roots = _set_allowed_roots(args.root)

    # All human readable startup output goes to stderr so it never corrupts the
    # stdio MCP protocol (which talks over stdout).
    toolnames = sorted(t.name for t in asyncio.run(mcp.list_tools()))

    # record a useful startup summary in the shared rotating log
    logging.info("=" * 70)
    logging.info("pyall MCP server starting")
    logging.info("version       : %s", __version__)
    logging.info("author        : %s", __author__)
    logging.info("start date    : %s", time.strftime("%Y-%m-%d %H:%M:%S"))
    logging.info("python        : %s", platform.python_version())
    logging.info("platform      : %s", platform.platform())
    logging.info("host machine  : %s", platform.node())
    logging.info("cpu count     : %s", os.cpu_count())
    logging.info("worker threads: %s (parallel request execution)", _MAXWORKERS)
    logging.info("transport     : %s", args.transport)
    if args.transport != "stdio":
        logging.info("endpoint      : http://%s:%d/mcp", args.host, args.port)
        logging.info("http sessions : stateful (per-client Mcp-Session-Id, concurrent)")
    logging.info("file access   : %s", ", ".join(roots) if roots else "unrestricted")
    logging.info("log file      : %s", LOGPATH)
    logging.info("abilities     : %d tools", len(toolnames))
    for name in toolnames:
        logging.info("    tool      : %s", name)
    logging.info("=" * 70)

    if args.transport == "stdio":
        print("pyall MCP server starting (stdio transport).", file=sys.stderr)
        print("It does not open a port or a URL - it talks to an MCP client over stdin/stdout.", file=sys.stderr)
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        _HTTP_ENDPOINT = (args.host, args.port)
        # Keep stateful HTTP sessions: every connecting client is issued its own
        # Mcp-Session-Id and the server services concurrent requests/sessions in
        # parallel via the worker thread pool.
        mcp.settings.stateless_http = False
        url = "http://%s:%d%s" % (
            args.host, args.port,
            mcp.settings.sse_path if args.transport == "sse" else mcp.settings.streamable_http_path,
        )
        print("pyall MCP server starting (%s transport)." % args.transport, file=sys.stderr)
        print("Listening on %s" % url, file=sys.stderr)
        monitor_url = "http://%s:%d%s" % (args.host, args.port, MONITOR_BASE)
        print("Monitor web page: %s" % monitor_url, file=sys.stderr)
        if args.host in ("0.0.0.0", "::"):
            print("Bound to all interfaces - reachable from other machines on the network.", file=sys.stderr)
            if not roots:
                print("WARNING: no --root configured. File system tools can read anywhere this "
                      "process can. Pass --root <folder> to confine access.", file=sys.stderr)

    if roots:
        print("File access confined to: %s" % ", ".join(roots), file=sys.stderr)
    else:
        print("File access is unrestricted (no --root configured).", file=sys.stderr)
    print("Exposing %d tools: %s" % (len(toolnames), ", ".join(toolnames)), file=sys.stderr)
    print("Logging to: %s" % LOGPATH, file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)
    sys.stderr.flush()

    mcp.run(transport=args.transport)

