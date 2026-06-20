# name:         pyall_mcp
# created:      June 2026
# by:           paul.kennedy@guardiangeomatics.com
# description:  Model Context Protocol (MCP) server exposing pyall point cloud generation tools.
#
# This server intentionally depends only on the consolidated `pyall` module for point cloud
# generation.  Concurrency is handled here with a thread pool (asyncio.to_thread) rather than
# multiprocessing.
#
# Run with:        python pyall_mcp.py
# (stdio transport, suitable for Claude Desktop / VS Code MCP clients)

import os
import time
import asyncio

import numpy as np

from mcp.server.fastmcp import FastMCP

import pyall

mcp = FastMCP("pyall")


###############################################################################
def _resolve_output_dir(input_file, output_dir):
    '''return an output folder, creating a timestamped one next to the input file when none is given.'''
    if output_dir:
        odir = output_dir
    else:
        odir = os.path.join(
            os.path.dirname(os.path.abspath(input_file)),
            "all2point_%s" % (time.strftime("%Y%m%d-%H%M%S")),
        )
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
    """Summarise a Kongsberg .all file.

    Returns datagram counts, the approximate position, file size and a suitable
    projected EPSG code for the survey area.

    Args:
        input_file: Absolute path to the .all file.
    """
    if not os.path.isfile(input_file):
        raise ValueError("File not found: %s" % input_file)
    return await asyncio.to_thread(pyall.getfileinfo, input_file)


###############################################################################
@mcp.tool()
async def depth_to_grid(input_file: str, resolution: float = 0, value: str = "depth",
                        colour: str = "none", epsg: str = "0", output_dir: str = "",
                        max_pings: int = -1, colour_min: float | None = None,
                        colour_max: float | None = None, keep_rejected: bool = False) -> dict:
    """Grid the bathymetry (or reflectivity) from a .all file into a GeoTIFF.

    Args:
        input_file: Absolute path to the .all file.
        resolution: Grid cell size in metres. 0 (default) auto-computes from the beam spacing, snapped to a sensible interval (0.5, 1, 2, 5, 10, ...).
        value: Quantity to grid: "depth" (default) or "reflectivity".
        colour: Rendering: "none" (float, default), "jeca" (colour ramp) or "grey" (greyscale).
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write the tif to. Empty writes next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        colour_min: Minimum value (same units as 'value', e.g. a depth) to stretch the palette across. None uses the full data range.
        colour_max: Maximum value to stretch the palette across. None uses the full data range.
        keep_rejected: When False (default) rejected soundings are excluded from the grid. True grids every sounding.
    """
    if not os.path.isfile(input_file):
        raise ValueError("File not found: %s" % input_file)

    tif = await asyncio.to_thread(
        pyall.depthtotif, input_file, resolution, value, colour, epsg, "", False,
        max_pings, False, output_dir, colour_min, colour_max, keep_rejected,
    )
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
async def all_to_tif(input_file: str, epsg: str = "0", output_dir: str = "",
                     max_pings: int = -1, verbose: bool = False) -> dict:
    """Read a .all file, build a bathymetric point cloud and rasterise it to a floating point GeoTIFF.

    Also writes the raw point cloud as a CSV (``<file>_R.txt``).

    Args:
        input_file: Absolute path to the .all file.
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write outputs to. Empty creates a timestamped folder next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        verbose: Enable verbose logging.
    """
    if not os.path.isfile(input_file):
        raise ValueError("File not found: %s" % input_file)

    odir = _resolve_output_dir(input_file, output_dir)
    params = _runtime_params(epsg, odir, max_pings, verbose)

    tif = await asyncio.to_thread(pyall.all2point, input_file, params)
    csv = os.path.join(odir, os.path.basename(input_file) + "_R.txt")
    return {
        'input_file': input_file,
        'epsg': params['epsg'],
        'output_dir': odir,
        'geotiff': tif,
        'pointcloud_csv': csv if os.path.isfile(csv) else None,
    }


###############################################################################
def _make_pointcloud_csv(input_file, params):
    '''load a .all file into a point cloud and write it to a CSV.  returns (csv_path, point_count).'''
    pointcloud = pyall.loaddata(input_file, params)
    count = len(pointcloud.xarr)
    csv = os.path.join(params['odir'], os.path.basename(input_file) + "_R.txt")
    if count == 0:
        return None, 0
    xyz = np.column_stack([
        pointcloud.xarr, pointcloud.yarr, pointcloud.zarr,
        pointcloud.qarr, pointcloud.rarr,
    ])
    np.savetxt(csv, xyz, fmt='%.10f', delimiter=',')
    return csv, count


###############################################################################
@mcp.tool()
async def all_to_pointcloud(input_file: str, epsg: str = "0", output_dir: str = "",
                            max_pings: int = -1, verbose: bool = False) -> dict:
    """Read a .all file and export the bathymetric point cloud as a CSV (no GeoTIFF).

    The CSV columns are: east, north, depth, quality, reflectivity.

    Args:
        input_file: Absolute path to the .all file.
        epsg: Output EPSG code. "0" (default) auto-detects a suitable projected CRS.
        output_dir: Folder to write outputs to. Empty creates a timestamped folder next to the input file.
        max_pings: Number of pings to process. -1 (default) processes all pings.
        verbose: Enable verbose logging.
    """
    if not os.path.isfile(input_file):
        raise ValueError("File not found: %s" % input_file)

    odir = _resolve_output_dir(input_file, output_dir)
    params = _runtime_params(epsg, odir, max_pings, verbose)
    if str(params['epsg']) == '0':
        params['epsg'] = str(pyall.getsuitableepsg(input_file))

    csv, count = await asyncio.to_thread(_make_pointcloud_csv, input_file, params)
    return {
        'input_file': input_file,
        'epsg': params['epsg'],
        'output_dir': odir,
        'pointcloud_csv': csv,
        'point_count': count,
    }


###############################################################################
@mcp.tool()
async def batch_process(input_folder: str, operation: str = "pointcloud", epsg: str = "0",
                        output_dir: str = "", max_pings: int = -1, recursive: bool = False,
                        max_concurrency: int = 4, resolution: float = 0, value: str = "depth",
                        colour: str = "none", colour_min: float | None = None,
                        colour_max: float | None = None, keep_rejected: bool = False) -> dict:
    """Process every .all file in a folder in parallel.

    Files are processed concurrently using a thread pool, bounded by max_concurrency, so a whole
    survey folder can be processed as efficiently as possible.

    Args:
        input_folder: Absolute path to a folder containing .all files.
        operation: "pointcloud" (default) writes a point cloud CSV + GeoTIFF per file;
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

    if not os.path.isdir(input_folder):
        raise ValueError("Folder not found: %s" % input_folder)
    if operation not in ("pointcloud", "grid"):
        raise ValueError("operation must be 'pointcloud' or 'grid', got: %s" % operation)

    matches = fileutils.findFiles2(bool(recursive), input_folder, "*.all")
    if len(matches) == 0:
        return {'input_folder': input_folder, 'operation': operation, 'processed': [], 'count': 0}

    if output_dir:
        odir = output_dir
    else:
        odir = os.path.join(input_folder, "all2point_%s" % (time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(odir, exist_ok=True)

    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def process_one(filename):
        async with semaphore:
            try:
                if operation == "grid":
                    tif = await asyncio.to_thread(
                        pyall.depthtotif, filename, resolution, value, colour, epsg, "", False,
                        max_pings, False, odir, colour_min, colour_max, keep_rejected,
                    )
                else:
                    params = _runtime_params(epsg, odir, max_pings, False)
                    tif = await asyncio.to_thread(pyall.all2point, filename, params)
                return {'input_file': filename, 'geotiff': tif, 'error': None}
            except Exception as ex:  # report per-file failures without aborting the batch
                return {'input_file': filename, 'geotiff': None, 'error': str(ex)}

    results = await asyncio.gather(*(process_one(f) for f in matches))
    succeeded = sum(1 for r in results if r['error'] is None)
    return {
        'input_folder': input_folder,
        'operation': operation,
        'output_dir': odir,
        'count': len(results),
        'succeeded': succeeded,
        'failed': len(results) - succeeded,
        'processed': results,
    }


###############################################################################
# datagram record access tools
###############################################################################

def _require_file(input_file):
    if not os.path.isfile(input_file):
        raise ValueError("File not found: %s" % input_file)


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
    recs = await asyncio.to_thread(pyall.loadpositions, input_file)
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
    arr = await asyncio.to_thread(pyall.loadattitude, input_file)
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
async def get_clock(input_file: str, max_records: int = 5000) -> dict:
    """Return clock (C) records for analysing clock stability (PC time vs external time and PPS).

    Args:
        input_file: Absolute path to the .all file.
        max_records: Maximum number of records to return (0 = all).
    """
    _require_file(input_file)
    recs = await asyncio.to_thread(pyall.loadclock, input_file)
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
    recs = await asyncio.to_thread(pyall.loadheight, input_file)
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
    recs = await asyncio.to_thread(pyall.loadsoundvelocityprofiles, input_file)
    return {'input_file': input_file, 'count': len(recs), 'profiles': recs}


###############################################################################
@mcp.tool()
async def get_surface_sound_speed(input_file: str) -> dict:
    """Return surface sound speed (G) datagrams, including mean/min/max sound speed in m/s.

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    recs = await asyncio.to_thread(pyall.loadsurfacesoundspeed, input_file)
    return {'input_file': input_file, 'count': len(recs), 'surfacesoundspeed': recs}


###############################################################################
@mcp.tool()
async def get_runtime_parameters(input_file: str) -> dict:
    """Return runtime parameter (R) records: decoded sonar settings (depth mode, filters, coverage, etc.).

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    recs = await asyncio.to_thread(pyall.loadruntimeparameters, input_file)
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
    recs = await asyncio.to_thread(pyall.loadtraveltime, input_file, max_records)
    return {'input_file': input_file, 'count': len(recs), 'traveltime': recs}


###############################################################################
@mcp.tool()
async def get_installation_parameters(input_file: str) -> dict:
    """Return the installation (I) datagram parameters (sensor offsets, serial numbers, etc.).

    Args:
        input_file: Absolute path to the .all file.
    """
    _require_file(input_file)
    info = await asyncio.to_thread(pyall.loadinstallationparameters, input_file)
    return {'input_file': input_file, 'installation': info}


###############################################################################
@mcp.tool()
async def get_depth(input_file: str, max_pings: int = 50, max_points: int = 20000) -> dict:
    """Return per-beam soundings from the X (and D) depth datagrams: depth, acrosstrack, alongtrack,
    reflectivity and quality.  These are large, so max_pings/max_points bound the response.

    Args:
        input_file: Absolute path to the .all file.
        max_pings: Maximum number of pings to read (-1 = all).
        max_points: Maximum number of beam points to return (0 = all read).
    """
    _require_file(input_file)
    d = await asyncio.to_thread(pyall.loaddepth, input_file, max_pings)
    total = int(d['depth'].shape[0])
    n = total
    truncated = False
    if max_points and max_points > 0 and total > max_points:
        n = max_points
        truncated = True
    return {
        'input_file': input_file,
        'count': total,
        'returned': n,
        'truncated': truncated,
        'columns': ['pingtimestamp', 'depth', 'acrosstrack', 'alongtrack', 'reflectivity', 'quality'],
        'pingtimestamp': d['pingtimestamp'][:n].tolist(),
        'depth': d['depth'][:n].tolist(),
        'acrosstrack': d['acrosstrack'][:n].tolist(),
        'alongtrack': d['alongtrack'][:n].tolist(),
        'reflectivity': d['reflectivity'][:n].tolist(),
        'quality': d['quality'][:n].tolist(),
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
    d = await asyncio.to_thread(pyall.loadseabedimage, input_file, max_pings)
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
    recs = await asyncio.to_thread(pyall.loadpustatus, input_file)
    data, total, truncated = _limit(recs, max_records)
    return {'input_file': input_file, 'count': total, 'truncated': truncated, 'pustatus': data}


###############################################################################
if __name__ == "__main__":
    import sys

    # The stdio transport uses stdout to talk to the MCP client, so all human
    # readable startup output must go to stderr to avoid corrupting the protocol.
    toolnames = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    print("pyall MCP server starting (stdio transport).", file=sys.stderr)
    print("It does not open a port or a URL - it talks to an MCP client over stdin/stdout.", file=sys.stderr)
    print("Exposing %d tools: %s" % (len(toolnames), ", ".join(toolnames)), file=sys.stderr)
    print("Waiting for an MCP client to connect... (press Ctrl+C to stop)", file=sys.stderr)
    print("To use it, point an MCP client (Claude Desktop, VS Code, etc.) at this script.", file=sys.stderr)
    sys.stderr.flush()

    mcp.run()
