# name:              qc.all
# created:        May 2018
# by:            paul.kennedy@guardiangeomatics.com
# description:   python module to read a Kongsberg ALL sonar file
# notes:             See main at end of script for example how to use this
# based on ALL Revision R October 2013

# See readme.md for more details

import sys
import math
import pprint
import struct
import os.path
import time
import io
import json
import re
import argparse
from datetime import datetime
from datetime import timedelta
import numpy as np

import geodetic
import logging
import logging.handlers
import threading
import timeseries
import ggmbes
import fileutils

# single source of truth for the package version (qcall_mcp re-exports this).  Bump on every change.
# It is also tagged into backscatter mosaic filenames so different algorithm versions are identifiable.
__version__ = "1.6.15"

###############################################################################
# per-thread progress hook
#
# Long processing runs (loaddata / depthtotif / backscattertotif) call _emitprogress() as they
# work through the pings.  A consumer (e.g. the MCP server) registers a hook with setprogresshook()
# on the SAME thread that runs the processing, so it receives a 0..1 fraction and a message and can
# stream it to its client.  The hook is thread-local so concurrent jobs do not cross-talk.
###############################################################################
_progresslocal = threading.local()

def setprogresshook(hook):
    '''register (or clear, with None) a progress callback hook(fraction0to1, message) for this thread.'''
    _progresslocal.hook = hook

def _emitprogress(fraction, message=''):
    '''report progress to the current thread's hook, if one is registered.  Never raises.'''
    hook = getattr(_progresslocal, 'hook', None)
    if hook is None:
        return
    try:
        f = float(fraction)
        f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
        hook(f, message)
    except Exception:
        pass

###############################################################################
def main():
    '''command line entry point.  Reads a Kongsberg .all file (or a folder of them) and creates point clouds and GeoTIFFs.'''

    parser = argparse.ArgumentParser(description='Read a Kongsberg ALL file and create a point cloud and GeoTIFF.')
    parser.add_argument('-i', action='store', default="", dest='inputfolder', help='Input filename/folder to process.')
    parser.add_argument('-epsg', action='store', default="0", dest='epsg', help='Specify an output EPSG code for transforming from WGS84 to East,North e.g. -epsg 32756')
    parser.add_argument('-odir', action='store', default="", dest='odir', help='Specify a relative output folder e.g. -odir GIS')
    parser.add_argument('-debug', action='store', default="-1", dest='debug', help='Specify the number of pings to process.  good only for debugging. [Default:-1, all]')
    parser.add_argument('-verbose', action='store_true', default=False, dest='verbose', help='verbose logging.  takes some additional time! [Default:false]')
    parser.add_argument('-info', action='store_true', default=False, dest='info', help='just report a summary of the file (datagram counts, position, EPSG) and exit.')
    parser.add_argument('-grid', action='store_true', default=False, dest='grid', help='grid the depth (or reflectivity) into a GeoTIFF instead of exporting a raw point cloud.')
    parser.add_argument('-resolution', action='store', default='0', dest='resolution', help='grid cell size in metres. 0 auto-computes from beam spacing.  only used with -grid. [Default:0]')
    parser.add_argument('-value', action='store', default='depth', dest='value', help='quantity to grid: depth or reflectivity.  only used with -grid. [Default:depth]')
    parser.add_argument('-colour', action='store', default='none', dest='colour', help='grid rendering: none (float), jeca (colour ramp) or grey (greyscale).  only used with -grid. [Default:none]')
    parser.add_argument('-colourmin', action='store', default='', dest='colourmin', help='minimum value for the colour/greyscale palette (e.g. depth range).  only used with -grid. [Default: full data range]')
    parser.add_argument('-colourmax', action='store', default='', dest='colourmax', help='maximum value for the colour/greyscale palette (e.g. depth range).  only used with -grid. [Default: full data range]')
    parser.add_argument('-keeprejected', action='store_true', default=False, dest='keeprejected', help='keep rejected soundings when gridding.  only used with -grid. [Default: rejected soundings are removed]')
    parser.add_argument('-noshade', action='store_true', default=False, dest='noshade', help='disable the default hillshade blended into colour/greyscale depth grids.  only used with -grid -value depth. [Default: hillshade on, sun 325 deg / 15 deg]')
    parser.add_argument('-vertical', action='store', default='waterline', dest='vertical', choices=['transducer', 'waterline', 'ellipsoid'], help="sounding vertical reference: 'transducer' (raw z), 'waterline' (add transducer depth) or 'ellipsoid' (also apply the Height datagram). [Default: waterline]")

    args = parser.parse_args()
    runtime_params = vars(args).copy()
    runtime_params.setdefault('spherical', False)

    matches = []
    if os.path.isfile(runtime_params['inputfolder']):
        matches.append(runtime_params['inputfolder'])
    elif os.path.isdir(runtime_params['inputfolder']):
        matches = fileutils.findFiles2(False, runtime_params['inputfolder'], "*.all")
    elif len(runtime_params['inputfolder']) == 0:
        # no file specified, so look in the current folder.
        matches = fileutils.findFiles2(False, os.getcwd(), "*.all")

    if len(matches) == 0:
        log("No input .all files found. Use -i <file-or-folder> or run in a folder containing .all files.", error=True)
        print("No input .all files found. Use -i <file-or-folder> or run in a folder containing .all files.")
        return

    # just report a summary of each file and exit
    if runtime_params['info']:
        for filename in matches:
            info = getfileinfo(filename)
            print("File: %s" % (info['filename']))
            print("  Size: %d bytes" % (info['filesize']))
            print("  Approx position: lon %.6f lat %.6f" % (info['approxlongitude'], info['approxlatitude']))
            if info.get('firstposition') and info.get('lastposition'):
                fp = info['firstposition']
                lp = info['lastposition']
                print("  First position: lon %.6f lat %.6f" % (fp['longitude'], fp['latitude']))
                print("  Last  position: lon %.6f lat %.6f" % (lp['longitude'], lp['latitude']))
            print("  Duration: %.0f s (%.2f hours)" % (info['durationseconds'], info['durationseconds'] / 3600.0))
            print("  Track distance: %.1f m (%.2f NM)" % (info['trackdistancemetres'], info['trackdistancenauticalmiles']))
            print("  Vessel speed: %.2f kn (%.2f m/s)" % (info['vesselspeedknots'], info['vesselspeedmps']))
            if info.get('courseovergrounddegrees') is not None:
                print("  Course over ground: %.1f deg" % (info['courseovergrounddegrees']))
            if info.get('approxwaterdepthm') is not None:
                print("  Approx water depth: %.1f m" % (info['approxwaterdepthm']))
            if info.get('centrefrequencyhz') is not None:
                print("  Centre frequency: %.0f Hz" % (info['centrefrequencyhz']))
            if info.get('swathcoveragedegrees') is not None:
                print("  Swath / sector angle: %d deg (port %d, stbd %d)" % (
                    info['swathcoveragedegrees'], info['portcoveragedegrees'], info['stbdcoveragedegrees']))
            if info.get('depthmode'):
                print("  Depth mode: %s" % (info['depthmode']))
            print("  Suitable EPSG: %s" % (info['epsg']))
            counts = ", ".join("%s:%d" % (k, v) for k, v in sorted(info['datagramcounts'].items()))
            print("  Datagrams: %s" % (counts))
        return

    # make sure we have a folder to write to
    runtime_params['inputfolder'] = os.path.dirname(matches[0])

    # make an output folder
    if len(runtime_params['odir']) == 0:
        runtime_params['odir'] = os.path.join(runtime_params['inputfolder'], str("all2point_%s" % (time.strftime("%Y%m%d-%H%M%S"))))
    if not os.path.isdir(runtime_params['odir']):
        os.makedirs(runtime_params['odir'], exist_ok=True)

    setup_logging()
    log("Configuration: %s" % (str(runtime_params)))
    log("Output Folder: %s" % (runtime_params['odir']))
    print("Output Folder: %s" % (runtime_params['odir']))

    # process each file sequentially.  multiprocessing is intentionally not used here;
    # callers that need concurrency (e.g. the MCP server) handle it themselves.
    requestedepsg = str(runtime_params['epsg'])
    for filename in matches:
        fileparams = runtime_params.copy()
        # reset the epsg per file so auto-detection works correctly for files in different zones
        fileparams['epsg'] = requestedepsg
        if runtime_params.get('grid', False):
            colourmin = float(runtime_params['colourmin']) if str(runtime_params.get('colourmin', '')) != '' else None
            colourmax = float(runtime_params['colourmax']) if str(runtime_params.get('colourmax', '')) != '' else None
            if str(runtime_params.get('value', 'depth')) == 'reflectivity':
                # backscatter uses the dedicated AVG-corrected mosaic path, not the bathymetry gridder
                outfilename = backscattertotif(filename, resolution=runtime_params['resolution'],
                                               epsg=requestedepsg, maxpings=int(runtime_params['debug']),
                                               verbose=runtime_params['verbose'], odir=runtime_params['odir'],
                                               colour=runtime_params['colour'],
                                               colourmin=colourmin, colourmax=colourmax,
                                               keeprejected=runtime_params['keeprejected'])
            else:
                outfilename = depthtotif(filename, resolution=runtime_params['resolution'],
                                         value=runtime_params['value'], colour=runtime_params['colour'],
                                         epsg=requestedepsg, maxpings=int(runtime_params['debug']),
                                         verbose=runtime_params['verbose'], odir=runtime_params['odir'],
                                         colourmin=colourmin, colourmax=colourmax,
                                         keeprejected=runtime_params['keeprejected'],
                                         shade=not runtime_params.get('noshade', False),
                                         vertical=runtime_params.get('vertical', 'waterline'))
        else:
            outfilename = all2point(filename, fileparams)
        if outfilename is not None:
            print("Created: %s" % (outfilename))


###############################################################################
def _get_runtime_param(runtime_params, key, default=None):
    if isinstance(runtime_params, dict):
        return runtime_params.get(key, default)
    return getattr(runtime_params, key, default)

###############################################################################
def _set_runtime_param(runtime_params, key, value):
    if isinstance(runtime_params, dict):
        runtime_params[key] = value
    else:
        setattr(runtime_params, key, value)

###############################################################################
def loaddata(filename, runtime_params):
    '''load a point cloud and return the cloud'''

    start_time = time.time() # time the process
    pointcloud = Cpointcloud()
    maxpings = int(_get_runtime_param(runtime_params, 'debug', -1))
    if maxpings == -1:
        maxpings = 999999999

    pingcounter = 0
    r = allreader(filename)

    # folder we report live status into (consumed by monitor.py)
    statusodir = _get_runtime_param(runtime_params, 'odir', '') or os.path.dirname(os.path.abspath(filename))

    epsg = str(_get_runtime_param(runtime_params, 'epsg', '0'))
    if epsg == '0':
        approxlongitude, approxlatitude = r.getapproximatepositon()
        epsg = geodetic.epsgfromlonglat(approxlongitude, approxlatitude)
        _set_runtime_param(runtime_params, 'epsg', epsg)

    #load the python proj projection object library if the user has requested it
    geo = geodetic.geodesy(epsg)
    verbose = bool(_get_runtime_param(runtime_params, 'verbose', False))
    
    #get the record count so we can show a progress bar
    recordcount, starttimestamp, endtimestamp = r.getrecordcount("X")

    writestatus(statusodir, state='loading', job='Extracting Point Cloud',
                file=os.path.basename(filename), progress=0.0, pings=0,
                recordcount=int(recordcount), epsg=str(epsg), elapsed=0.0)

    #we need to load the navigation to we can compute the position of the transducer at ping time...
    navigation = r.loadnavigation()
    nav = np.array(navigation)
    tslatitude = timeseries.cTimeSeries(nav[:,0], nav[:,1])
    tslongitude = timeseries.cTimeSeries(nav[:,0], nav[:,2])

    # vertical reference for the soundings.  The XYZ88 z is relative to the transmit transducer, so:
    #   'transducer' -> leave z as-is (raw, re transducer face)
    #   'waterline'  -> add the transducer depth (draft + heave at ping) so z is re the water line  [default]
    #   'ellipsoid'  -> additionally remove the Height (h) datagram so z is re the ellipsoid (GPS tide)
    vertical = str(_get_runtime_param(runtime_params, 'vertical', 'waterline')).lower()
    tsheight = None
    if vertical == 'ellipsoid':
        heights = loadheight(filename)
        if len(heights) >= 1:
            ht = np.array([[h['timestamp'], h['height']] for h in heights], dtype=float)
            tsheight = timeseries.cTimeSeries(ht[:, 0], ht[:, 1])
        else:
            log("vertical=ellipsoid requested but no Height (h) datagrams found; using water line instead", error=True)
            vertical = 'waterline'
    log("Vertical reference: %s" % (vertical))

    # demonstrate how to load the navigation records into a list.  this is really handy if we want to make a trackplot for coverage
    while r.moredata():
        # read a datagram.  If we support it, return the datagram type and aclass for that datagram
        # The user then needs to call the read() method for the class to undertake a fileread and binary decode.  This keeps the read super quick.
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'X':
            datagram.read()
            datagram.timestamp = to_timestamp(to_datetime(datagram.recorddate, datagram.time))
            datagram.latitude = tslatitude.getValueAt(datagram.timestamp)
            datagram.longitude = tslongitude.getValueAt(datagram.timestamp)
            if verbose:
                logging.info("Processing ping %d (position loaded)" % (pingcounter + 1))
            # per-ping vertical offset (metres, positive down) onto the chosen datum
            if vertical == 'transducer':
                zoffset = 0.0
            else:
                zoffset = float(getattr(datagram, 'transducerdepth', 0.0) or 0.0)
                if vertical == 'ellipsoid' and tsheight is not None:
                    zoffset -= float(tsheight.getValueAt(datagram.timestamp))
            x, y, z, q, r_reflectivity = computebathypointcloud(datagram, geo, zoffset)
            pointcloud.add(x, y, z, q, r_reflectivity)
            update_progress("Extracting Point Cloud", pingcounter/recordcount)
            pingcounter = pingcounter + 1
            _emitprogress(pingcounter / recordcount if recordcount else 0.0, "Extracting point cloud")
            writestatus(statusodir, throttle=0.5, state='processing', job='Extracting Point Cloud',
                        file=os.path.basename(filename),
                        progress=(pingcounter / recordcount) if recordcount else 0.0,
                        pings=pingcounter, recordcount=int(recordcount), epsg=str(epsg),
                        elapsed=time.time() - start_time)

        if pingcounter == maxpings:
            break

    r.close()
    log("Load Duration: %.3f seconds" % (time.time() - start_time))
    writestatus(statusodir, state='loaded', job='Extracting Point Cloud',
                file=os.path.basename(filename), progress=1.0, pings=pingcounter,
                recordcount=int(recordcount), epsg=str(epsg), elapsed=time.time() - start_time)

    return pointcloud

###############################################################################
def update_progress(job_title, progress):
    '''progress value should be a value between 0 and 1'''
    length = 20 # modify this to change the length
    block = int(round(length*progress))
    msg = "\r{0}: [{1}] {2}%".format(job_title, "#"*block + "-"*(length-block), round(progress*100, 2))
    if progress >= 1: msg += " DONE\r\n"
    sys.stdout.write(msg)
    sys.stdout.flush()

###############################################################################
def    log(msg, error = False, printmsg=True):
        if error == False:
            logging.info(msg)
        else:
            logging.error(msg)

###############################################################################
# centralised rotating log + live status (shared by the CLI and the MCP server)
###############################################################################
LOGFILENAME = 'qcall.log'
STATUSFILENAME = 'qcall_status.json'
_statusfile_lastwrite = 0.0

###############################################################################
def logdirectory():
    '''return the folder that holds the shared rotating log and the central status file.
    Override with the QCALL_LOG_DIR environment variable; defaults to a "logs" folder
    next to this module so the CLI, the MCP server and the monitor all agree on it.'''
    d = os.environ.get('QCALL_LOG_DIR', '')
    if not d:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    return d

###############################################################################
def setup_logging(logdir=None, level=logging.INFO, maxbytes=5 * 1024 * 1024, backups=5):
    '''configure the root logger to write to a single rotating log file shared by the whole
    toolkit (CLI runs and the MCP server).  Safe to call repeatedly.  Returns the log path.'''
    if logdir is None:
        logdir = logdirectory()
    try:
        os.makedirs(logdir, exist_ok=True)
    except OSError:
        pass
    logpath = os.path.join(logdir, LOGFILENAME)
    root = logging.getLogger()
    root.setLevel(level)
    # only add our rotating handler once, even if called many times
    for h in root.handlers:
        if getattr(h, '_qcall_rotating', False):
            return logpath
    handler = logging.handlers.RotatingFileHandler(
        logpath, maxBytes=maxbytes, backupCount=backups, encoding='utf-8')
    handler._qcall_rotating = True
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    root.addHandler(handler)
    return logpath

###############################################################################
def statusfilepath(odir):
    '''return the path of the status json file written inside an output folder.'''
    return os.path.join(odir, STATUSFILENAME) if odir else ''

###############################################################################
def _writestatusfile(path, fields):
    '''atomically write a status dict to a single json file.  never raises.'''
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(fields, f)
        os.replace(tmp, path)
    except Exception:
        pass

###############################################################################
def writestatus(odir, throttle=0.0, **fields):
    '''record live processing status so the monitor web page can display progress.
    Writes a central status file (in logdirectory(), which the monitor watches) and, when
    an output folder is given, a copy inside that folder.  This never raises - status
    reporting must not interfere with processing.'''
    global _statusfile_lastwrite
    try:
        now = time.time()
        # avoid hammering the disk when called every ping
        if throttle and (now - _statusfile_lastwrite) < throttle and fields.get('state') == 'processing':
            return
        _statusfile_lastwrite = now
        fields.setdefault('updated', now)
        # central status - the one place the monitor always looks
        _writestatusfile(os.path.join(logdirectory(), STATUSFILENAME), fields)
        # per-output-folder copy for browsing a specific job folder
        if odir:
            _writestatusfile(statusfilepath(odir), fields)
    except Exception:
        pass

###############################################################################
def computebathypointcloud(datagram, geo, zoffset=0.0):
    '''using the depth datagram, efficiently compute numpy arrays of the point cloud.

    zoffset (metres, positive down) is added to every beam depth to move the soundings from the
    transmit-transducer reference onto the chosen vertical datum (water line or ellipsoid).'''

    datagram.east, datagram.north = geo.convertToGrid((datagram.longitude), datagram.latitude)
    # detection / realtime-cleaning flags let us know which soundings have been rejected
    detinfo = getattr(datagram, 'detectioninformation', None)
    rtclean = getattr(datagram, 'realtimecleaninginformation', None)
    for idx in range(datagram.nbeams):

        beam = ggmbes.GGBeam()
        # depth from the telegram is re the transmit transducer; zoffset references it to the chosen datum
        beam.depth = datagram.depth[idx] + zoffset
        beam.east, beam.north = geodetic.calculateGridPositionFromBearingDxDy(datagram.east, datagram.north, datagram.heading, datagram.acrosstrackdistance[idx], datagram.alongtrackdistance[idx])

        beam.backscatter   = datagram.reflectivity[idx]
        # bit 7 of the detection information flags an invalid/bad detection; a negative realtime cleaning
        # value flags a beam that has been cleaned out.  capture either as the rejected flag (bit 7).
        rej = int(detinfo[idx]) if detinfo is not None else 0
        if rtclean is not None and rtclean[idx] < 0:
            rej = rej | 0x80
        beam.rejectionInfo1 = rej
        datagram.beams.append(beam)

    npeast = np.fromiter((beam.east for beam in datagram.beams), float, count=len(datagram.beams)) #. Also, adding count=len(stars)
    npnorth = np.fromiter((beam.north for beam in datagram.beams), float, count=len(datagram.beams)) #. Also, adding count=len(stars)
    npdepth = np.fromiter((beam.depth for beam in datagram.beams), float, count=len(datagram.beams)) #. Also, adding count=len(stars)
    npq = np.fromiter((beam.rejectionInfo1 for beam in datagram.beams), float, count=len(datagram.beams)) #. Also, adding count=len(stars)
    npreflectivity = np.fromiter((beam.backscatter for beam in datagram.beams), float, count=len(datagram.beams)) #. Also, adding count=len(stars)

    # we can now comput absolute positions from the relative positions
    # npLatitude_deg = npdeltaLatitude_deg + datagram.latitude_deg    
    # npLongitude_deg = npdeltaLongitude_deg + datagram.longitude_deg
    return (npeast, npnorth, npdepth, npq, npreflectivity)


###############################################################################
def getsuitableepsg(filename):
    '''load the first position record and return the EPSG code for the position'''
    r = allreader(filename)
    approxlongitude, approxlatitude = r.getapproximatepositon()
    epsg = geodetic.epsgfromlonglat(approxlongitude, approxlatitude)
    r.close()
    return epsg


###############################################################################
def _savexyzcsv(path, east, north, depth, quality, reflectivity):
    '''write the raw point cloud CSV (east,north,depth,quality,reflectivity) at sensible precision.

    Uses a single combined format string so np.savetxt does one format per row instead of one per
    field, and drops the old fake %.10f precision (1 mm is plenty for projected metres) - this is
    faster and produces a much smaller file on the millions of soundings in a large .all file.'''
    xyz = np.column_stack([
        np.asarray(east, dtype=float), np.asarray(north, dtype=float),
        np.asarray(depth, dtype=float), np.asarray(quality, dtype=float),
        np.asarray(reflectivity, dtype=float)])
    np.savetxt(path, xyz, fmt='%.3f,%.3f,%.3f,%.0f,%.2f')


###############################################################################
def all2point(filename, runtime_params):
    '''process a single .all file, create a point cloud and export it to a CSV (_R.txt) and a GeoTIFF.
    returns the path to the created GeoTIFF, or None if no data could be extracted.'''
    # lazy import so that simply reading datagrams does not require rasterio/scipy
    import cloud2tif

    # load the proj projection object.  Auto-detect a suitable EPSG if the user did not specify one.
    epsg = str(_get_runtime_param(runtime_params, 'epsg', '0'))
    if epsg == '0':
        epsg = str(getsuitableepsg(filename))
        _set_runtime_param(runtime_params, 'epsg', epsg)
    geo = geodetic.geodesy(str(epsg))

    log("Processing file: %s" % (filename))
    pointcloud = loaddata(filename, runtime_params)
    if len(pointcloud.xarr) == 0:
        log("No point cloud data extracted from %s" % (filename), error=True)
        return None

    xyz = np.column_stack([pointcloud.xarr, pointcloud.yarr, pointcloud.zarr, pointcloud.qarr, pointcloud.rarr])

    odir = _get_runtime_param(runtime_params, 'odir', '') or os.path.dirname(filename)
    if len(odir) > 0 and not os.path.isdir(odir):
        os.makedirs(odir, exist_ok=True)

    # report on RAW POINTS - fast vectorised CSV writer (np.savetxt is far too slow on large files)
    outfile = os.path.join(odir, os.path.basename(filename) + "_R.txt")
    _savexyzcsv(outfile, pointcloud.xarr, pointcloud.yarr, pointcloud.zarr, pointcloud.qarr, pointcloud.rarr)

    # rasterise the point cloud into a floating point GeoTIFF
    outfilename = os.path.join(outfile + "_Raw_depth.tif")
    cloud2tif.saveastif(outfilename, geo, xyz, resolution=2, fill=False)

    log("Read complete at: %s" % (datetime.now()))
    writestatus(odir, state='done', job='Extracting Point Cloud',
                file=os.path.basename(filename), progress=1.0, epsg=str(epsg),
                geotiff=outfilename, pointcloud_csv=outfile)
    return outfilename


###############################################################################
def snapresolution(value):
    '''snap a raw resolution (metres) up to the next sensible grid interval (0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, ...).'''
    ladder = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0]
    for step in ladder:
        if value <= step:
            return step
    return ladder[-1]

###############################################################################
def computeapproximateresolution(filename, samplepings=10, coarsen=1.5):
    '''estimate a sensible grid resolution (metres) from the across-track beam spacing of the first few depth pings.
    the raw median spacing is multiplied by *coarsen* (default 1.5) and then snapped up to the next sensible
    interval (0.5, 1, 2, 5, 10, ...).  The mild coarsening keeps a couple of soundings per cell so the auto grid
    is not speckled/holed, while staying close to the true beam spacing - raw spacing alone grids too fine and
    a large factor grids too coarse.'''
    r = allreader(filename)
    spacings = []
    pingcount = 0
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram in ('X', 'D'):
            datagram.read()
            ac = [a for a in datagram.acrosstrackdistance if not math.isnan(a)]
            if len(ac) > 1:
                spread = max(ac) - min(ac)
                if spread > 0:
                    spacings.append(spread / (len(ac) - 1))
            pingcount += 1
            if pingcount >= samplepings:
                break
    r.close()
    if len(spacings) == 0:
        return 1.0
    return snapresolution(max(float(np.median(spacings)) * coarsen, 0.01))

###############################################################################
def loadcolourramp(rampfilename=""):
    '''load an RGB colour ramp (one "R G B" triplet per line, 0-255) and return a numpy array shaped (N, 3).
    defaults to the jeca.txt ramp shipped alongside this module.'''
    if len(rampfilename) == 0:
        rampfilename = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jeca.txt")
    ramp = []
    with open(rampfilename, 'r') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3:
                ramp.append([int(parts[0]), int(parts[1]), int(parts[2])])
    return np.array(ramp, dtype=np.uint8)

###############################################################################
def _gridtomean(east, north, value, resolution):
    '''bin the (east, north, value) points into a regular grid and return the per-cell mean.
    returns (meanarray, countarray, (xmin, ymin), (xmax, ymax)).  rows run north (top) to south (bottom).'''
    xy = np.vstack([east, north])
    mnoriginal = xy.min(axis=1)
    mxoriginal = xy.max(axis=1)

    # quantise into cells working in centimetres so non-integer resolutions are supported
    xycm = xy * 100.0
    xybin = ((xycm + (resolution * 100.0) / 2.0) // (resolution * 100.0)).astype(int)
    mn = xybin.min(axis=1)
    mx = xybin.max(axis=1)
    sz = mx + 1 - mn

    flatidx = np.ravel_multi_index(xybin - mn[:, None], dims=sz)
    sums = np.bincount(flatidx, value, sz.prod())
    counts = np.bincount(flatidx, None, sz.prod())
    mean = sums / np.maximum(1, counts)

    meanarr = np.flip(mean.reshape(sz).T, axis=0)
    countarr = np.flip(counts.reshape(sz).T, axis=0)
    return meanarr, countarr, mnoriginal, mxoriginal


###############################################################################
def _gridtostat(east, north, value, resolution, stat='mean', trim=0.1):
    '''bin (east, north, value) points into a regular grid and reduce each cell with *stat*:

      'mean'    - arithmetic mean (fast path, identical to _gridtomean).
      'median'  - per-cell median; robust to the bright/dark specular outliers that streak the nadir.
      'trimmed' - mean after dropping the top/bottom *trim* fraction of each cell's values.

    The median/trimmed reducers knock down short-term specular speckle so a few flashes in a cell no
    longer pull its value up.  returns (array, countarray, (xmin, ymin), (xmax, ymax)); rows run north
    (top) to south (bottom).'''
    xy = np.vstack([east, north])
    mnoriginal = xy.min(axis=1)
    mxoriginal = xy.max(axis=1)

    xycm = xy * 100.0
    xybin = ((xycm + (resolution * 100.0) / 2.0) // (resolution * 100.0)).astype(int)
    mn = xybin.min(axis=1)
    mx = xybin.max(axis=1)
    sz = mx + 1 - mn
    flatidx = np.ravel_multi_index(xybin - mn[:, None], dims=sz)
    ncells = int(sz.prod())
    counts = np.bincount(flatidx, None, ncells)

    if stat == 'mean':
        sums = np.bincount(flatidx, value, ncells)
        out = sums / np.maximum(1, counts)
    else:
        # sort points by (cell, value) so each cell's values are contiguous and ascending
        order = np.lexsort((value, flatidx))
        sf = flatidx[order]
        sv = value[order]
        cells, starts, cnts = np.unique(sf, return_index=True, return_counts=True)
        if stat == 'trimmed':
            lo = starts + np.floor(cnts * float(trim)).astype(int)
            hi = starts + np.ceil(cnts * (1.0 - float(trim))).astype(int)
            hi = np.maximum(hi, lo + 1)                  # always keep at least one value
            csum = np.concatenate(([0.0], np.cumsum(sv)))
            vals = (csum[hi] - csum[lo]) / (hi - lo)
        else:  # median (default for any non-mean/non-trimmed request)
            midlo = starts + (cnts - 1) // 2
            midhi = starts + cnts // 2
            vals = 0.5 * (sv[midlo] + sv[midhi])
        out = np.zeros(ncells)
        out[cells] = vals

    arr = np.flip(out.reshape(sz).T, axis=0)
    countarr = np.flip(counts.reshape(sz).T, axis=0)
    return arr, countarr, mnoriginal, mxoriginal


###############################################################################
def _infillinteriorholes(arr, mask, maxgapcells=1.5, maxsearchcells=2.0):
    '''interpolate the small empty cells a finer grid leaves between soundings, without smearing the
    swath edge.

    Every nodata cell whose distance to the nearest real sounding cell is <= *maxgapcells* is filled
    by a short-range interpolation (rasterio fillnodata, search radius *maxsearchcells*).  Because the
    inter-sounding stipple gaps are ~1 cell wide they are all filled, while the open nodata beyond the
    swath is many cells from data and is left alone - so the edge keeps its natural ragged outline and
    is not stretched outward.  Needs scipy; if unavailable the array is returned unchanged.
    returns (arr, mask).'''
    if not mask.any() or not (~mask).any():
        return arr, mask
    try:
        from scipy import ndimage
    except Exception:
        return arr, mask
    import rasterio.fill
    dist = ndimage.distance_transform_edt(mask)              # cells: distance to nearest data cell
    region = mask & (dist <= float(maxgapcells))
    if not region.any():
        return arr, mask
    filled = rasterio.fill.fillnodata(np.where(mask, 0.0, arr).astype('float32'),
                                      mask=(~mask).astype('uint8'),
                                      max_search_distance=float(maxsearchcells), smoothing_iterations=0)
    newly = region & np.isfinite(filled) & (filled != 0.0)
    if newly.any():
        arr = np.where(newly, filled, arr)
        mask = mask & ~newly
    return arr, mask


###############################################################################
def _despikealongtrack(backscatter, pingid, beamid, window, angles=None, nadirwindow=0, nadirzonedeg=8.0):
    '''suppress along-track speckle with a running-median FIFO, optionally stronger near nadir.

    Builds a [ping x beam] grid of backscatter and replaces each value with the median of the same
    beam over a sliding window of consecutive pings.  A median (not mean) window removes transient
    specular flashes while preserving the seabed trend.

    The near-nadir beams are far noisier than the outer swath: the specular response is a sharp,
    slightly off-nadir peak, so tiny per-sounding beam-angle errors map to large backscatter errors
    and streak the nadir.  When *angles* is given and *nadirwindow* > *window*, beams whose mean
    |angle-from-nadir| is below *nadirzonedeg* use the longer *nadirwindow* (heavily smoothing the
    unreliable, heavily-overlapped nadir strip) while the outer beams keep the short *window* (so
    outer-swath resolution is preserved).  returns a despiked copy of *backscatter*.'''
    window = int(window)
    nadirwindow = int(nadirwindow)
    if window < 3 and nadirwindow < 3:
        return backscatter
    npings = int(pingid.max()) + 1
    nbeams = int(beamid.max()) + 1
    grid = np.full((npings, nbeams), np.nan, dtype=float)
    valid = np.isfinite(backscatter) & (backscatter != 0)
    grid[pingid[valid], beamid[valid]] = backscatter[valid]

    # choose a median window per beam column: longer for near-nadir beams when requested
    colwindow = np.full(nbeams, max(window, 0), dtype=int)
    if angles is not None and nadirwindow > window:
        anggrid = np.full((npings, nbeams), np.nan, dtype=float)
        anggrid[pingid[valid], beamid[valid]] = angles[valid]
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            meanabsang = np.nanmean(np.abs(anggrid), axis=0)
        nadircols = np.isfinite(meanabsang) & (meanabsang < float(nadirzonedeg))
        colwindow[nadircols] = nadirwindow

    from numpy.lib.stride_tricks import sliding_window_view
    out = grid.copy()
    import warnings
    with np.errstate(all='ignore'):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)   # all-NaN windows -> NaN, handled below
            # filter each group of columns that share a window in one vectorised pass
            for win in np.unique(colwindow):
                if win < 3:
                    continue
                w = win if win % 2 else win + 1            # force odd, centred
                cols = np.where(colwindow == win)[0]
                half = w // 2
                sub = grid[:, cols]
                padded = np.pad(sub, ((half, half), (0, 0)), constant_values=np.nan)
                chunk = max(500, 4_000_000 // max(1, len(cols) * w))   # bound peak memory
                for s in range(0, npings, chunk):
                    e = min(npings, s + chunk)
                    sw = sliding_window_view(padded[s:e + 2 * half], w, axis=0)  # (e-s, len(cols), w)
                    out[s:e, cols] = np.nanmedian(sw, axis=2)

    despiked = backscatter.copy()
    med = out[pingid, beamid]
    use = np.isfinite(med)
    despiked[use] = med[use]
    return despiked

###############################################################################
def depthtotif(filename, resolution=0.0, value='depth', colour='none', epsg='0', outfilename='', fill=False, maxpings=-1, verbose=False, odir='', colourmin=None, colourmax=None, keeprejected=False, shade=True, shadeazimuth=325.0, shadealtitude=15.0, vertical='waterline'):
    '''grid the bathymetry from a .all file into a GeoTIFF.

    resolution : grid cell size in metres.  0 (default) auto-computes a value from the approximate beam spacing,
                 snapped to a sensible interval (0.5, 1, 2, 5, 10, ...).
    value      : 'depth' (default) or 'reflectivity' - the quantity to grid.
    colour     : 'none' (default) -> single band floating point tif.
                 'jeca'           -> 3 band RGB tif coloured with the jeca.txt ramp.
                 'grey'           -> 3 band greyscale RGB tif.
    epsg       : output EPSG code.  '0' auto-detects a suitable projected CRS.
    odir       : output folder for the auto-named tif.  empty writes next to the input file.
    colourmin / colourmax : value range (same units as 'value') to stretch the colour/greyscale palette across.
                 None (default) uses the full data range of each file.
    keeprejected : when False (default) soundings flagged as rejected (bad detection or cleaned out) are
                 excluded from the grid.  set True to grid every sounding regardless of its quality flag.
    shade      : when True (default) a hillshade is blended over a colour/greyscale depth raster for shaded
                 relief.  Ignored for the single band float output (colour='none').
    shadeazimuth / shadealtitude : hillshade sun direction (deg from north) and elevation (deg). [Default: 325, 15]
    vertical   : sounding vertical reference - 'transducer' (raw), 'waterline' (add transducer depth, default)
                 or 'ellipsoid' (also remove the Height datagram).
    returns the path to the created GeoTIFF, or None if no data could be extracted.'''
    from rasterio.transform import from_origin

    # work out the grid resolution
    if resolution is None or float(resolution) <= 0:
        resolution = computeapproximateresolution(filename)
        log("Auto-computed grid resolution: %.3f m" % (resolution))
    resolution = float(resolution)

    epsg = str(epsg)
    if epsg == '0':
        epsg = str(getsuitableepsg(filename))
    geo = geodetic.geodesy(str(epsg))

    runtime_params = {'epsg': epsg, 'debug': str(maxpings), 'verbose': bool(verbose), 'spherical': False, 'odir': '', 'vertical': vertical}
    log("Loading point cloud for gridding...")
    pointcloud = loaddata(filename, runtime_params)
    if len(pointcloud.xarr) == 0:
        log("No point cloud data extracted from %s" % (filename), error=True)
        return None

    east = np.array(pointcloud.xarr, dtype=float)
    north = np.array(pointcloud.yarr, dtype=float)
    if value == 'reflectivity':
        val = np.array(pointcloud.rarr, dtype=float)
    else:
        val = np.array(pointcloud.zarr, dtype=float)

    # drop rejected soundings (bit 7 of the quality/rejection flag) unless the caller asks to keep them
    if not keeprejected:
        qual = np.array(pointcloud.qarr, dtype=float)
        if qual.size == east.size:
            keep = (qual.astype(np.int64) & 0x80) == 0
            rejectedcount = int((~keep).sum())
            if rejectedcount > 0:
                log("Excluding %d rejected soundings from the grid" % (rejectedcount))
            east = east[keep]
            north = north[keep]
            val = val[keep]
    if east.size == 0:
        log("No accepted soundings to grid for %s" % (filename), error=True)
        return None

    arr, countarr, (xmin, ymin), (xmax, ymax) = _gridtomean(east, north, val, resolution)
    mask = countarr == 0

    height, width = arr.shape
    transform = from_origin(xmin - resolution / 2.0, ymax + resolution / 2.0, resolution, resolution)

    if len(outfilename) == 0:
        rendering = colour if colour != 'none' else 'float'
        suffix = "_%s_%s_%gm.tif" % (value, rendering, resolution)
        targetdir = odir if len(odir) > 0 else os.path.dirname(filename)
        if len(targetdir) > 0 and not os.path.isdir(targetdir):
            os.makedirs(targetdir, exist_ok=True)
        outfilename = os.path.join(targetdir, os.path.basename(filename) + suffix)

    # hillshade is only meaningful for a depth surface - never shade reflectivity/backscatter
    shade = shade and value == 'depth'

    return _savegridtotif(outfilename, arr, mask, geo, transform, resolution,
                          colour=colour, colourmin=colourmin, colourmax=colourmax, fill=fill,
                          shade=shade, shadeazimuth=shadeazimuth, shadealtitude=shadealtitude)


###############################################################################
def _hillshade(array, azimuth=325.0, altitude=15.0, cellsize=1.0):
    '''compute shaded relief (0..1) from a 2d elevation/depth array.

    azimuth   : direction of the illuminating sun, degrees clockwise from north. [Default: 325]
    altitude  : sun elevation above the horizon, degrees. [Default: 15]
    cellsize  : grid cell size (metres) so the shading is consistent at any resolution.'''
    az = 360.0 - float(azimuth)
    x, y = np.gradient(array, float(cellsize) if cellsize else 1.0)
    slope = np.pi / 2.0 - np.arctan(np.sqrt(x * x + y * y))
    aspect = np.arctan2(-x, y)
    azm = math.radians(az)
    alt = math.radians(float(altitude))
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos((azm - np.pi / 2.0) - aspect)
    return np.clip((shaded + 1.0) / 2.0, 0.0, 1.0)


###############################################################################
def _savegridtotif(outfilename, arr, mask, geo, transform, resolution, colour='none', colourmin=None, colourmax=None, fill=False, shade=False, shadeazimuth=325.0, shadealtitude=15.0, stretch='percentile', stretchsigma=2.5, clippercent=2.5, gamma=1.0):
    '''write a gridded 2d array to a GeoTIFF.  colour='none' -> single band float; 'jeca' -> RGB colour
    ramp; 'grey' -> RGB greyscale.  Masked (empty) cells are nodata/black.  Shared by the bathymetry and
    backscatter gridders.  When *shade* is set, a hillshade (default sun azimuth 325 deg, altitude 15 deg)
    is blended over the colour/greyscale render to give shaded relief.'''
    import rasterio
    height, width = arr.shape
    log("Creating grid tif file... %s (%d x %d @ %.3f m)" % (outfilename, width, height, resolution))

    if colour == 'none':
        # single band floating point
        NODATA = -999.0
        out = np.where(mask, NODATA, arr).astype('float32')
        with rasterio.open(outfilename, 'w', driver='GTiff', height=height, width=width,
                           count=1, dtype='float32', crs=geo.projection.srs, transform=transform,
                           nodata=NODATA, compress='deflate', zlevel=9) as dst:
            if fill:
                from rasterio.fill import fillnodata
                out = fillnodata(out, mask=(~mask).astype('uint8'), max_search_distance=resolution * 2, smoothing_iterations=0)
            dst.write(out, 1)
    else:
        # 3 band RGB (coloured ramp or greyscale)
        valid = arr[~mask]
        # work out the palette range.  Explicit colourmin/colourmax always win; otherwise *stretch*
        # selects the mechanism: 'stddev' (mean +/- stretchsigma*std - a gentle, low-contrast stretch
        # for backscatter), 'percentile' (clip the clippercent tails) or 'minmax'.
        if colourmin is not None:
            vmin = float(colourmin)
        elif not valid.size:
            vmin = 0.0
        elif stretch == 'stddev':
            vmin = float(valid.mean() - float(stretchsigma) * valid.std())
        elif stretch == 'minmax':
            vmin = float(valid.min())
        else:
            vmin = float(np.percentile(valid, float(clippercent)))
        if colourmax is not None:
            vmax = float(colourmax)
        elif not valid.size:
            vmax = 1.0
        elif stretch == 'stddev':
            vmax = float(valid.mean() + float(stretchsigma) * valid.std())
        elif stretch == 'minmax':
            vmax = float(valid.max())
        else:
            vmax = float(np.percentile(valid, 100.0 - float(clippercent)))
        if vmax <= vmin:
            vmax = vmin + 1.0
        log("Colour palette range: %.3f to %.3f (%s)" % (vmin, vmax, stretch))
        norm = np.clip((arr - vmin) / (vmax - vmin), 0.0, 1.0)
        if gamma and float(gamma) != 1.0:
            norm = norm ** float(gamma)
        idx = (norm * 255.0).astype(int)
        idx = np.clip(idx, 0, 255)

        if colour == 'jeca':
            ramp = loadcolourramp()
            n = ramp.shape[0] - 1
            rampidx = np.clip((norm * n).astype(int), 0, n)
            rgb = ramp[rampidx]  # (h, w, 3)
            red = rgb[:, :, 0].astype('uint8')
            green = rgb[:, :, 1].astype('uint8')
            blue = rgb[:, :, 2].astype('uint8')
        else:  # grey
            grey = idx.astype('uint8')
            red = green = blue = grey

        # blend in shaded relief (hillshade) so the colour depth raster shows topography
        if shade:
            base = arr.astype(float).copy()
            if mask.any() and (~mask).any():
                base[mask] = float(arr[~mask].mean())   # avoid cliff artefacts at the swath edge / holes
            relief = _hillshade(base, shadeazimuth, shadealtitude, resolution)
            red = np.clip(red.astype(float) * relief, 0, 255).astype('uint8')
            green = np.clip(green.astype(float) * relief, 0, 255).astype('uint8')
            blue = np.clip(blue.astype(float) * relief, 0, 255).astype('uint8')

        # nodata cells are rendered black
        red = np.where(mask, 0, red).astype('uint8')
        green = np.where(mask, 0, green).astype('uint8')
        blue = np.where(mask, 0, blue).astype('uint8')

        with rasterio.open(outfilename, 'w', driver='GTiff', height=height, width=width,
                           count=3, dtype='uint8', crs=geo.projection.srs, transform=transform,
                           photometric='RGB', compress='deflate', zlevel=9) as dst:
            dst.write(red, 1)
            dst.write(green, 2)
            dst.write(blue, 3)

    log("Creating grid tif file Complete.")
    return outfilename


###############################################################################
# persistent Angular Varied Gain (AVG) store
#
# The seabed angular backscatter response is a property of the sonar (transducer serial number) and
# the acoustic mode it was running in (depth mode), not of any single survey line.  A single .all
# file rarely contains enough soundings to characterise the sharp nadir response, so the per-file
# AVG leaves a bright nadir spike behind.  To fix this we accumulate the angular response (per-bin
# sum and count of backscatter) on disc keyed by (serialnumber, depthmode) and grow it with every
# file processed.  The running mean of the accumulated store is a steadily improving AVG curve that
# resolves the nadir spike and removes it from the mosaic.
###############################################################################
_AVG_ANGLE_MIN = -90.0          # canonical angle-from-nadir grid so every file bins identically
_AVG_ANGLE_MAX = 90.0
_avgstorelocks = {}             # one lock per store file for safe read-modify-write under threading
_avgstorelocksguard = threading.Lock()


def _defaultavgdir():
    '''default folder for the persistent AVG store (next to this module).'''
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'avgcache')


def _avgsanitise(text):
    '''make a serial number / depth mode string safe to embed in a filename.'''
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(text)).strip('_')
    return s or 'unknown'


def _avgstorelock(storefile):
    '''return (creating if needed) the lock guarding a particular AVG store file.'''
    with _avgstorelocksguard:
        lk = _avgstorelocks.get(storefile)
        if lk is None:
            lk = threading.Lock()
            _avgstorelocks[storefile] = lk
        return lk


def _avgcanonicaledges(binsize):
    '''fixed angle-from-nadir bin edges shared by every file so stores are mergeable.'''
    binsize = float(binsize) if binsize and binsize > 0 else 1.0
    edges = np.arange(_AVG_ANGLE_MIN, _AVG_ANGLE_MAX + binsize, binsize)
    return edges, binsize


def avgstorepath(avgdir, serialnumber, depthmode, binsize=0.5):
    '''build the on-disc path for the AVG store of one (serialnumber, depthmode) at a given bin size.'''
    avgdir = avgdir or _defaultavgdir()
    name = "avg_%s_%s_%gdeg.json" % (_avgsanitise(serialnumber), _avgsanitise(depthmode), float(binsize))
    return os.path.join(avgdir, name)


def _loadavgstore(storefile, nbins, binsize):
    '''read the accumulated per-bin (sums, counts) from disc; zeros if missing or incompatible.'''
    if storefile and os.path.isfile(storefile):
        try:
            with open(storefile, 'r') as f:
                d = json.load(f)
            if int(d.get('nbins', -1)) == nbins and float(d.get('binsize', 0)) == float(binsize):
                return (np.asarray(d['sums'], dtype=float), np.asarray(d['counts'], dtype=float))
        except Exception:
            pass
    return np.zeros(nbins, dtype=float), np.zeros(nbins, dtype=float)


def _saveavgstore(storefile, sums, counts, binsize, serialnumber, depthmode):
    '''atomically write the accumulated per-bin (sums, counts) back to disc.'''
    if not storefile:
        return
    os.makedirs(os.path.dirname(storefile), exist_ok=True)
    payload = {
        'serialnumber': str(serialnumber), 'depthmode': str(depthmode),
        'binsize': float(binsize), 'nbins': int(len(sums)),
        'anglemin': _AVG_ANGLE_MIN, 'anglemax': _AVG_ANGLE_MAX,
        'totalsamples': int(counts.sum()),
        'sums': [float(x) for x in sums], 'counts': [float(x) for x in counts],
        'updated': time.time(),
    }
    tmp = storefile + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f)
    os.replace(tmp, storefile)


def _trendfromstore(sums, counts, centres):
    '''running-mean AVG trend from accumulated (sums, counts); empty bins interpolated.'''
    nbins = len(counts)
    trend = np.full(nbins, np.nan)
    nonempty = counts > 0
    trend[nonempty] = sums[nonempty] / counts[nonempty]
    if nonempty.sum() >= 2:
        trend = np.interp(centres, centres[nonempty], trend[nonempty])
    elif nonempty.sum() == 1:
        trend[:] = float(trend[nonempty][0])
    else:
        trend[:] = 0.0
    return trend


def accumulateangularvariedgain(angles, backscatter, storefile, binsize=0.5,
                                serialnumber='', depthmode='', minfilesamples=50):
    '''accumulate this file's angular response into the persistent store and return an AVG trend that
    flattens THIS line's own across-swath response, falling back to the accumulated store only where
    the file is too thin to characterise a bin.

    The nadir specular strength varies line-to-line (depth / seabed type) even within one depth mode,
    so a pooled multi-file mean under-corrects any line whose nadir is brighter than average and leaves
    a bright nadir stripe.  Each line is therefore flattened against its OWN per-angle mean wherever it
    has at least *minfilesamples* soundings in a bin (true for every near-nadir bin on a normal line),
    and the running store mean is used only to fill sparse bins on short files.  The store is still
    grown with every file (so it steadily improves and remains available for thin lines).

    returns (centres, trend, edges, totalsamples).'''
    edges, binsize = _avgcanonicaledges(binsize)
    nbins = len(edges) - 1
    centres = edges[:-1] + binsize / 2.0

    # bin this file's soundings on the canonical grid
    idx = np.clip(np.digitize(angles, edges) - 1, 0, nbins - 1)
    filesums = np.bincount(idx, backscatter, nbins)
    filecounts = np.bincount(idx, None, nbins)

    if storefile:
        with _avgstorelock(storefile):
            storesums, storecounts = _loadavgstore(storefile, nbins, binsize)
            storesums = storesums + filesums
            storecounts = storecounts + filecounts
            _saveavgstore(storefile, storesums, storecounts, binsize, serialnumber, depthmode)
    else:
        storesums, storecounts = filesums, filecounts

    # prefer this file's own per-angle mean (smooth despite the 0.5 dB reflectivity quantisation and
    # adapts to this line); fall back to the accumulated store mean only for bins it barely samples
    trend = np.full(nbins, np.nan)
    usefile = filecounts >= int(minfilesamples)
    trend[usefile] = filesums[usefile] / filecounts[usefile]
    usestore = (~usefile) & (storecounts > 0)
    trend[usestore] = storesums[usestore] / storecounts[usestore]
    known = np.isfinite(trend)
    if known.sum() >= 2:
        trend = np.interp(centres, centres[known], trend[known])
    elif known.sum() == 1:
        trend[:] = float(trend[known][0])
    else:
        trend[:] = 0.0
    return centres, trend, edges, int(storecounts.sum())


###############################################################################
def computeangularvariedgain(angles, backscatter, binsize=0.5):
    '''build the Angular Varied Gain (AVG) trend for a swath of backscatter.

    The seabed reflects more energy near nadir than at the outer beams, so raw backscatter has a
    strong dependence on beam angle.  This bins every sounding by its beam angle from nadir (port
    negative, starboard positive) and returns the mean backscatter per angle bin - the angular
    response curve that the mosaic must be flattened against.  The mean (not median) is used because
    the recorded reflectivity is quantised to 0.5 dB steps, and a per-bin median snaps to those steps
    and bands the mosaic, whereas the mean averages the quantisation out into a smooth curve.

    returns (bincentres, trend, edges); trend[i] is the mean backscatter in angle bin i (empty bins
    are linearly interpolated from their neighbours).'''
    binsize = float(binsize) if binsize and binsize > 0 else 1.0
    amin = math.floor(float(angles.min()) / binsize) * binsize
    amax = math.ceil(float(angles.max()) / binsize) * binsize
    if amax <= amin:
        amax = amin + binsize
    edges = np.arange(amin, amax + binsize, binsize)
    nbins = len(edges) - 1
    idx = np.clip(np.digitize(angles, edges) - 1, 0, nbins - 1)
    sums = np.bincount(idx, backscatter, nbins)
    counts = np.bincount(idx, None, nbins)
    trend = np.full(nbins, np.nan)
    nonempty = counts > 0
    trend[nonempty] = sums[nonempty] / counts[nonempty]
    centres = edges[:-1] + binsize / 2.0
    # fill empty bins so every sounding has a defined correction
    if nonempty.sum() >= 2:
        trend = np.interp(centres, centres[nonempty], trend[nonempty])
    elif nonempty.sum() == 1:
        trend[:] = float(trend[nonempty][0])
    else:
        trend[:] = 0.0
    return centres, trend, edges


###############################################################################
def _loadbackscatterpoints(filename, runtime_params):
    '''load per-beam seabed backscatter with map position and beam angle from nadir.

    returns numpy arrays: east, north, backscatter (dB), angle (deg, port -ve / stbd +ve) and a
    rejected boolean mask, followed by the transducer serial number (I datagram) and depth mode
    (R datagram) used to key the persistent AVG store, and finally per-sounding ping index and beam
    index (used by the along-track despike FIFO).  The beam angle is derived from the across-track
    offset and the sounding depth (atan2(acrosstrack, depth)), which is the incidence angle on a
    locally flat seabed.'''
    start_time = time.time()
    maxpings = int(_get_runtime_param(runtime_params, 'debug', -1))
    if maxpings == -1:
        maxpings = 999999999

    r = allreader(filename)
    statusodir = _get_runtime_param(runtime_params, 'odir', '') or os.path.dirname(os.path.abspath(filename))

    epsg = str(_get_runtime_param(runtime_params, 'epsg', '0'))
    if epsg == '0':
        approxlongitude, approxlatitude = r.getapproximatepositon()
        epsg = geodetic.epsgfromlonglat(approxlongitude, approxlatitude)
        _set_runtime_param(runtime_params, 'epsg', epsg)
    geo = geodetic.geodesy(str(epsg))

    recordcount, starttimestamp, endtimestamp = r.getrecordcount("X")
    navigation = r.loadnavigation()
    nav = np.array(navigation)
    tslatitude = timeseries.cTimeSeries(nav[:, 0], nav[:, 1])
    tslongitude = timeseries.cTimeSeries(nav[:, 0], nav[:, 2])

    writestatus(statusodir, state='loading', job='Backscatter AVG mosaic',
                file=os.path.basename(filename), progress=0.0, pings=0,
                recordcount=int(recordcount), epsg=str(epsg), elapsed=0.0)

    easts, norths, bs, angles, rejected = [], [], [], [], []
    pingids, beamids = [], []   # per-sounding ping index and beam index for the along-track despike FIFO
    pingts, pinge, pingn, pinghdg = [], [], [], []   # per-ping time/position/heading for speed & turn-rate
    serialnumber = None        # transducer serial (I datagram) - keys the persistent AVG store
    depthmode = None           # acoustic depth mode (R datagram) - keys the persistent AVG store
    pingcounter = 0
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'I' and serialnumber is None:
            datagram.read()
            serialnumber = datagram.serialnumber
        elif typeofdatagram == 'R' and depthmode is None:
            datagram.read()
            depthmode = datagram.depthmode
        elif typeofdatagram in ('X', 'D'):
            datagram.read()
            ts = to_timestamp(to_datetime(datagram.recorddate, datagram.time))
            lat = tslatitude.getValueAt(ts)
            lon = tslongitude.getValueAt(ts)
            e0, n0 = geo.convertToGrid(lon, lat)
            detinfo = getattr(datagram, 'detectioninformation', None)
            rtclean = getattr(datagram, 'realtimecleaninginformation', None)
            heading = datagram.heading
            pingts.append(ts); pinge.append(e0); pingn.append(n0); pinghdg.append(heading)
            for idx in range(datagram.nbeams):
                depth = datagram.depth[idx]
                across = datagram.acrosstrackdistance[idx]
                along = datagram.alongtrackdistance[idx]
                e, n = geodetic.calculateGridPositionFromBearingDxDy(e0, n0, heading, across, along)
                easts.append(e)
                norths.append(n)
                bs.append(datagram.reflectivity[idx])
                angles.append(math.degrees(math.atan2(across, abs(depth))) if depth else 0.0)
                pingids.append(pingcounter)
                beamids.append(idx)
                rej = int(detinfo[idx]) if detinfo is not None else 0
                if rtclean is not None and rtclean[idx] < 0:
                    rej |= 0x80
                rejected.append((rej & 0x80) != 0)
            pingcounter += 1
            update_progress("Backscatter AVG mosaic", pingcounter / recordcount if recordcount else 0.0)
            _emitprogress(pingcounter / recordcount if recordcount else 0.0, "Loading backscatter")
            writestatus(statusodir, throttle=0.5, state='processing', job='Backscatter AVG mosaic',
                        file=os.path.basename(filename),
                        progress=(pingcounter / recordcount) if recordcount else 0.0,
                        pings=pingcounter, recordcount=int(recordcount), epsg=str(epsg),
                        elapsed=time.time() - start_time)
            if pingcounter >= maxpings:
                break
    r.close()
    writestatus(statusodir, state='loaded', job='Backscatter AVG mosaic',
                file=os.path.basename(filename), progress=1.0, pings=pingcounter,
                recordcount=int(recordcount), epsg=str(epsg), elapsed=time.time() - start_time)

    # per-ping vessel speed (m/s) and heading turn-rate (deg/s) from the ping positions/headings,
    # used to trim line turns and slow-downs where the backscatter geometry is unreliable
    pt = np.array(pingts, dtype=float)
    pe = np.array(pinge, dtype=float)
    pn = np.array(pingn, dtype=float)
    ph = np.array(pinghdg, dtype=float)
    npings = pt.size
    pingspeed = np.zeros(npings, dtype=float)
    pingturn = np.zeros(npings, dtype=float)
    if npings >= 2:
        dt = np.diff(pt)
        dt[dt <= 0] = np.nan
        sp = np.hypot(np.diff(pe), np.diff(pn)) / dt
        dh = (np.diff(ph) + 180.0) % 360.0 - 180.0          # wrap heading change to +/-180
        tr = np.abs(dh) / dt
        pingspeed[1:] = sp
        pingspeed[0] = sp[0] if sp.size else 0.0
        pingturn[1:] = tr
        pingturn[0] = tr[0] if tr.size else 0.0
        # smooth over a few pings so a single noisy nav/heading sample does not trim a whole ping
        if npings >= 5:
            from numpy.lib.stride_tricks import sliding_window_view
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                pad = lambda a: np.pad(a, 2, mode='edge')
                pingspeed = np.nanmedian(sliding_window_view(pad(pingspeed), 5), axis=1)
                pingturn = np.nanmedian(sliding_window_view(pad(pingturn), 5), axis=1)
    pingspeed = np.nan_to_num(pingspeed, nan=0.0)
    pingturn = np.nan_to_num(pingturn, nan=0.0)

    return (np.array(easts, dtype=float), np.array(norths, dtype=float),
            np.array(bs, dtype=float), np.array(angles, dtype=float),
            np.array(rejected, dtype=bool), serialnumber, depthmode,
            np.array(pingids, dtype=np.int64), np.array(beamids, dtype=np.int64),
            pingspeed, pingturn)


###############################################################################
def backscattertotif(filename, resolution=0.0, epsg='0', outfilename='', maxpings=-1, verbose=False, odir='', colour='grey', colourmin=None, colourmax=None, anglebinsize=0.5, keeprejected=False, avgdir='', useavgstore=True, applyavg=True, gridstat='mean', despikepings=0, nadirdespikepings=0, nadirzonedeg=8.0, nadirmaskdeg=2.5, nadirfill=True, minspeedmps=0.0, maxturnratedegs=0.0, greystretch='minmax', greysigma=2.5, greygamma=1.0, infill=True):
    '''grid seabed backscatter (reflectivity) into a GeoTIFF with an Angular Varied Gain (AVG) correction.

    Backscatter is NOT gridded the same way as bathymetry.  A raw mosaic shows a bright nadir stripe and
    dark swath edges because the seabed returns more energy near nadir than at grazing angles.  This
    method characterises that angular response (mean backscatter vs beam angle from nadir), then
    subtracts the trend from each sounding - normalising every beam angle back to the survey mean -
    before gridding.  The result is a radiometrically balanced mosaic with the across-track angular
    seam removed.

    The angular response is a property of the sonar and its acoustic mode, but its nadir strength also
    varies line-to-line with depth and seabed type, so each line is flattened against its OWN per-angle
    response (which removes that line's nadir spike).  When *useavgstore* is True the response is also
    accumulated to disc keyed by the transducer serial number (installation 'I' datagram) and depth
    mode (runtime 'R' datagram); that growing store is used only to fill angle bins a short line cannot
    populate on its own.

    The residual nadir imbalance is NOT random speckle that smoothing can fix - it is consistent
    specular energy in the innermost beams.  The specular peak is sharp and slightly off-nadir, the
    recorded near-nadir reflectivity is unreliable, and a mean AVG cannot remove it (along-track
    smoothing only reinforces it into a bright stripe).  The robust solution is to drop the nadir
    beams entirely (*nadirmaskdeg*) - exactly as production backscatter mosaickers do - and interpolate
    across the resulting thin gap (*nadirfill*); overlapping survey lines then fill it completely in a
    combined mosaic.

    resolution    : grid cell size in metres.  0 (default) auto-computes a coarse value from beam spacing.
    colour        : 'grey' (default) greyscale, 'jeca' colour ramp, or 'none' for a single band float tif.
    anglebinsize  : width in degrees of the AVG angle bins. [Default: 0.5]
    keeprejected  : keep soundings flagged as rejected. [Default: drop them]
    avgdir        : folder for the persistent AVG store.  Empty uses an 'avgcache' folder beside this module.
    useavgstore   : when True (default) accumulate and reuse the AVG curve on disc keyed by
                    (serialnumber, depthmode).  False reverts to a per-file AVG only.
    applyavg      : when True (default) apply the AVG correction.  False grids the raw (uncorrected)
                    backscatter - a useful reference mosaic showing the nadir stripe before AVG.
    gridstat      : per-cell reducer - 'mean' (default, smooth tones; reflectivity is 0.5 dB quantised so
                    'median' posterises the contrast), 'median' or 'trimmed' (10%% trimmed mean).
    despikepings  : optional light along-track running-median spike filter (pings) applied to all beams.
                    [Default: 0, off]  Smooths along-track and lowers apparent resolution, so off by default.
    nadirmaskdeg  : drop soundings within this many degrees of nadir (the unreliable specular zone) so
                    the bright nadir line is removed. [Default: 2.5]  0 keeps the nadir.
    nadirfill     : when True (default) interpolate backscatter across the thin nadir gap left by
                    *nadirmaskdeg* so a single line has no gap.  Ignored when *nadirmaskdeg* is 0.
    infill        : when True (default) interpolate across empty cells fully enclosed by data (the
                    scattered single-cell gaps left by the finer auto grid), leaving swath edges alone.
    nadirdespikepings : optional longer along-track median window on the near-nadir beams (within
                    *nadirzonedeg*). [Default: 0, off]  Not recommended - it reinforces the nadir stripe;
                    prefer *nadirmaskdeg*.
    nadirzonedeg  : half-width (deg from nadir) of the beams treated as nadir for *nadirdespikepings*. [Default: 8]
    minspeedmps   : drop pings where the vessel was slower than this (m/s) - trims line-start/turn
                    slow-downs where swaths pile up. [Default: 0, off]
    maxturnratedegs : drop pings where the heading turn-rate exceeds this (deg/s) - trims line turns.
                    [Default: 0, off]
    greystretch   : greyscale tone mapping - 'minmax' (default, full-range linear: smooth, low-contrast,
                    keeps dark-area detail), 'percentile' (clip the tails) or 'stddev' (mean +/- greysigma*std).
                    colourmin/colourmax override it.
    greysigma     : standard deviations each side of the mean for the 'stddev' stretch. [Default: 2.5]
    greygamma     : gamma applied to the 0..1 tones.  <1 lifts the shadows, >1 darkens. [Default: 1.0, linear]
    returns the path to the created GeoTIFF, or None if no data could be extracted.'''
    from rasterio.transform import from_origin

    if resolution is None or float(resolution) <= 0:
        # backscatter carries fine texture worth keeping, so grid close to the raw beam spacing
        # (coarsen 1.0) rather than the coarser default used for bathymetry
        resolution = computeapproximateresolution(filename, coarsen=1.0)
        log("Auto-computed grid resolution: %.3f m" % (resolution))
    resolution = float(resolution)

    epsg = str(epsg)
    if epsg == '0':
        epsg = str(getsuitableepsg(filename))
    geo = geodetic.geodesy(str(epsg))

    runtime_params = {'epsg': epsg, 'debug': str(maxpings), 'verbose': bool(verbose), 'spherical': False, 'odir': odir}
    log("Loading seabed backscatter for AVG correction...")
    east, north, backscatter, angles, rejected, serialnumber, depthmode, pingids, beamids, pingspeed, pingturn = _loadbackscatterpoints(filename, runtime_params)
    if east.size == 0:
        log("No backscatter extracted from %s" % (filename), error=True)
        return None

    # ---- along-track running-median FIFO: knock down per-ping speckle (stronger near nadir) ----
    if (despikepings and int(despikepings) >= 3) or (nadirdespikepings and int(nadirdespikepings) >= 3):
        backscatter = _despikealongtrack(backscatter, pingids, beamids, despikepings,
                                         angles=angles, nadirwindow=int(nadirdespikepings),
                                         nadirzonedeg=nadirzonedeg)
        if int(nadirdespikepings) > int(despikepings):
            log("Applied angle-adaptive despike (outer %d pings, nadir %d pings within %.1f deg)" % (
                int(despikepings), int(nadirdespikepings), float(nadirzonedeg)))
        else:
            log("Applied along-track running-median despike over %d pings" % (int(despikepings)))

    # keep finite, non-zero soundings; drop rejected unless asked to keep them
    keep = np.isfinite(backscatter) & np.isfinite(angles) & (backscatter != 0)
    if not keeprejected:
        keep &= ~rejected
    # trim line turns / slow-downs where the swath geometry is unreliable (opt-in)
    if (minspeedmps and minspeedmps > 0) or (maxturnratedegs and maxturnratedegs > 0):
        badping = np.zeros(pingspeed.size, dtype=bool)
        if minspeedmps and minspeedmps > 0:
            badping |= pingspeed < float(minspeedmps)
        if maxturnratedegs and maxturnratedegs > 0:
            badping |= pingturn > float(maxturnratedegs)
        if badping.any():
            trimmed = int(badping[pingids].sum())
            keep &= ~badping[pingids]
            log("Trimmed %d soundings from %d slow/turning pings" % (trimmed, int(badping.sum())))
    # coordinates of every accepted sounding (including nadir) - used to confine the nadir gap-fill
    # to genuine interior holes so the swath edges are never extrapolated outward
    covereast, covernorth = east[keep], north[keep]
    domask = bool(nadirmaskdeg and float(nadirmaskdeg) > 0)
    if domask:
        masked = int((np.abs(angles[keep]) < float(nadirmaskdeg)).sum())
        keep &= np.abs(angles) >= float(nadirmaskdeg)
        log("Masked %d soundings within %.1f deg of nadir" % (masked, float(nadirmaskdeg)))
    east, north, backscatter, angles = east[keep], north[keep], backscatter[keep], angles[keep]
    if east.size == 0:
        log("No accepted backscatter samples to grid for %s" % (filename), error=True)
        return None

    # ---- Angular Varied Gain: characterise then flatten the across-swath angular response ----
    if not applyavg:
        # reference mosaic: grid the raw backscatter with no AVG correction (still shows the nadir stripe)
        corrected = backscatter
        log("AVG correction disabled - gridding raw backscatter (%d samples)" % (backscatter.size))
    else:
        if useavgstore:
            storefile = avgstorepath(avgdir, serialnumber, depthmode, anglebinsize)
            centres, trend, edges, totalsamples = accumulateangularvariedgain(
                angles, backscatter, storefile, anglebinsize, serialnumber, depthmode)
            log("AVG store %s (serial %s, mode %s) now holds %d samples" % (
                os.path.basename(storefile), serialnumber, depthmode, totalsamples))
        else:
            centres, trend, edges = computeangularvariedgain(angles, backscatter, anglebinsize)
        globallevel = float(np.mean(backscatter))   # add back the regional level so values keep their dB scale
        nbins = len(edges) - 1
        binidx = np.clip(np.digitize(angles, edges) - 1, 0, nbins - 1)
        corrected = backscatter - trend[binidx] + globallevel
        log("Applied AVG correction over %d angle bins of %.1f deg (survey mean %.2f dB)" % (nbins, float(anglebinsize), globallevel))

    arr, countarr, (xmin, ymin), (xmax, ymax) = _gridtostat(east, north, corrected, resolution, stat=gridstat)
    mask = countarr == 0
    log("Gridded backscatter with per-cell '%s' statistic" % (gridstat))

    # interpolate across the thin nadir gap left by nadirmaskdeg so a single line has no gap.
    # the fill is confined to interior cells that HAD coverage before the nadir mask, so the swath
    # edges (genuine nodata) are never extrapolated outward.
    if domask and nadirfill and mask.any():
        _, covcount, _, _ = _gridtostat(covereast, covernorth, np.zeros(covereast.size), resolution, stat='mean')
        if covcount.shape == countarr.shape:
            fillregion = mask & (covcount > 0)
            if fillregion.any():
                import rasterio.fill
                filled = rasterio.fill.fillnodata(np.where(mask, 0.0, arr).astype('float32'),
                                                  mask=(~mask).astype('uint8'),
                                                  max_search_distance=20.0, smoothing_iterations=0)
                newly = fillregion & np.isfinite(filled) & (filled != 0.0)
                if newly.any():
                    arr = np.where(newly, filled, arr)
                    mask = mask & ~newly
                    log("Filled %d interior nadir-gap cells by interpolation" % (int(newly.sum())))

    # close the scattered single-cell gaps left by the finer auto grid (interior holes only)
    if infill and mask.any():
        before = int(mask.sum())
        arr, mask = _infillinteriorholes(arr, mask)
        closed = before - int(mask.sum())
        if closed > 0:
            log("Infilled %d interior empty cells" % (closed))

    height, width = arr.shape
    transform = from_origin(xmin - resolution / 2.0, ymax + resolution / 2.0, resolution, resolution)

    if len(outfilename) == 0:
        rendering = colour if colour != 'none' else 'float'
        kind = "avg" if applyavg else "raw"
        # tag the algorithm version into the name so different versions are easy to compare
        suffix = "_backscatter_%s_%s_%gm_v%s.tif" % (kind, rendering, resolution, __version__)
        targetdir = odir if len(odir) > 0 else os.path.dirname(filename)
        if len(targetdir) > 0 and not os.path.isdir(targetdir):
            os.makedirs(targetdir, exist_ok=True)
        outfilename = os.path.join(targetdir, os.path.basename(filename) + suffix)

    out = _savegridtotif(outfilename, arr, mask, geo, transform, resolution,
                         colour=colour, colourmin=colourmin, colourmax=colourmax,
                         stretch=greystretch, stretchsigma=greysigma, gamma=greygamma)
    writestatus(odir or os.path.dirname(filename), state='done', job='Backscatter AVG mosaic',
                file=os.path.basename(filename), progress=1.0, epsg=str(epsg), geotiff=out)
    return out


###############################################################################
def _haversinemetres(lat1, lon1, lat2, lon2):
    '''great-circle distance between two WGS84 lat/lon points, in metres.'''
    radius = 6378137.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2.0) ** 2
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(a)))

###############################################################################
def _initialbearingdeg(lat1, lon1, lat2, lon2):
    '''initial great-circle bearing from point 1 to point 2, in degrees (0-360).'''
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

###############################################################################
def _runtimeparametersdict(datagram):
    '''build the human-friendly runtime parameter dictionary from a decoded R_RUNtime datagram.'''
    return {
        'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
        'emmodel': datagram.emmodel,
        'serialnumber': datagram.serialnumber,
        'depthmode': datagram.depthmode,
        'txpulseform': datagram.TXPulseForm,
        'dualswathmode': datagram.dualSwathMode,
        'filtersetting': datagram.filterSetting,
        'minimumdepth': datagram.minimumdepth,
        'maximumdepth': datagram.maximumdepth,
        'absorptioncoefficient': datagram.absorptionCoefficient,
        'transmitpulselength': datagram.transmitPulseLength,
        'transmitbeamwidth': datagram.transmitBeamWidth,
        'transmitpower': datagram.transmitPower,
        'receivebeamwidth': datagram.receiveBeamWidth,
        'beamspacing': datagram.beamSpacingString,
        'maximumportwidth': datagram.maximumPortWidth,
        'maximumportcoveragedegrees': datagram.maximumPortCoverageDegrees,
        'maximumstbdwidth': datagram.maximumStbdWidth,
        'maximumstbdcoveragedegrees': datagram.maximumStbdCoverageDegrees,
        'yawstabilisation': datagram.yawAndPitchStabilisationMode,
    }

###############################################################################
def getfileinfo(filename):
    '''read a .all file and return a rich but lightweight summary dictionary.

    It scans only the datagram headers (seeking over the bodies), decodes every position (P)
    record inline, and decodes just ONE each of the depth (X/D), travel-time (N) and runtime (R)
    records to report representative values.  It never decodes the full bathymetry and never writes
    a point cloud or GeoTIFF, so it stays fast even on very large files.

    Returns datagram counts, file size, first/last position, survey duration, track distance,
    average vessel speed, course over ground, approximate water depth, centre frequency, swath
    coverage / sector angle and a suitable projected EPSG code.'''
    filesize = os.path.getsize(filename)

    header_unpack = allreader.allpacketheader_unpack   # '=LBBHLL'
    header_len = allreader.allpacketheader_len         # 16 bytes
    p_struct = struct.Struct('=LBBHLLHHll4HBB')         # P record fixed part (lat=s[8], lon=s[9], sog=s[11])
    p_size = p_struct.size

    counts = {}
    positions = []          # (timestamp, lat, lon, sog_mps_or_None)
    selecteddescriptor = None   # lock onto a single positioning system (like loadnavigation)
    firstbytes = {}         # raw bytes of the first X/D/N/R record we meet
    attituderph = []        # first (roll, pitch, heave) of each A record - for significant attitude
    wanted = ('X', 'D', 'N', 'R')
    starttimestamp = 0
    endtimestamp = 0
    first = True

    with open(filename, 'rb') as f:
        pos = 0
        while pos + header_len <= filesize:
            f.seek(pos, 0)
            head = f.read(header_len)
            if len(head) < header_len:
                break
            try:
                s = header_unpack(head)
            except struct.error:
                break

            numberofbytes = s[0] + 4
            typeofdatagram = chr(s[2])
            recorddate = s[4]
            recordtime = float(s[5] / 1000.0)

            # stop on a corrupt / truncated trailing record rather than mis-scanning
            if numberofbytes < header_len or pos + numberofbytes > filesize:
                break

            ts = to_timestamp(to_datetime(recorddate, recordtime))
            if first:
                starttimestamp = ts
                first = False
            endtimestamp = ts
            counts[typeofdatagram] = counts.get(typeofdatagram, 0) + 1

            if typeofdatagram == 'P' and numberofbytes >= p_size:
                ps = p_struct.unpack(head + f.read(p_size - header_len))
                lat = ps[8] / 20000000.0
                lon = ps[9] / 10000000.0
                descriptor = ps[14]
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    # only keep one positioning system so the track does not zig-zag between sources
                    if selecteddescriptor is None:
                        selecteddescriptor = descriptor
                    if descriptor == selecteddescriptor:
                        sog = ps[11] / 100.0 if ps[11] != 65535 else None   # 65535 == not available
                        positions.append((ts, lat, lon, sog))
            elif typeofdatagram in wanted and typeofdatagram not in firstbytes:
                f.seek(pos, 0)
                firstbytes[typeofdatagram] = f.read(numberofbytes)
            elif typeofdatagram == 'A' and numberofbytes >= 32:
                # take just the first entry's roll/pitch/heave (int16 @ offsets 26/28/30, 0.01 units)
                arec = head + f.read(32 - header_len)
                if struct.unpack_from('=H', arec, 20)[0] > 0:
                    attituderph.append(struct.unpack_from('=hhh', arec, 26))

            pos += numberofbytes

    # ---- positions / track ----
    longitude = positions[0][2] if positions else 0.0
    latitude = positions[0][1] if positions else 0.0
    firstposition = None
    lastposition = None
    trackmetres = 0.0
    meanspeed_mps = 0.0
    course_deg = None
    if positions:
        firstposition = {'timestamp': positions[0][0], 'latitude': positions[0][1], 'longitude': positions[0][2]}
        lastposition = {'timestamp': positions[-1][0], 'latitude': positions[-1][1], 'longitude': positions[-1][2]}
        for i in range(1, len(positions)):
            trackmetres += _haversinemetres(positions[i - 1][1], positions[i - 1][2], positions[i][1], positions[i][2])
        sogs = [p[3] for p in positions if p[3]]
        if sogs:
            meanspeed_mps = sum(sogs) / len(sogs)
        course_deg = _initialbearingdeg(positions[0][1], positions[0][2], positions[-1][1], positions[-1][2])

    durationseconds = (endtimestamp - starttimestamp) if (endtimestamp and starttimestamp) else 0.0
    if meanspeed_mps == 0.0 and durationseconds > 0:
        meanspeed_mps = trackmetres / durationseconds

    # ---- sample one of each interesting record (no full bathy processing) ----
    def _decode(recbytes, cls):
        dg = cls(io.BytesIO(recbytes), len(recbytes))
        dg.read()
        return dg

    approxdepth = None
    centrefrequency = None
    depthmode = None
    portcoverage = None
    stbdcoverage = None
    runtimeparameters = None

    depthbytes = firstbytes.get('X') or firstbytes.get('D')
    if depthbytes:
        try:
            dg = _decode(depthbytes, X_depth if firstbytes.get('X') else D_depth)
            depths = sorted(d for d in dg.depth if d and d == d)   # drop zero / NaN
            if depths:
                approxdepth = depths[len(depths) // 2]              # median sounding
        except Exception:
            pass

    if firstbytes.get('N'):
        try:
            dg = _decode(firstbytes['N'], N_TRAVELtime)
            if dg.centrefrequency and dg.centrefrequency[0]:
                centrefrequency = float(dg.centrefrequency[0])
        except Exception:
            pass

    if firstbytes.get('R'):
        try:
            dg = _decode(firstbytes['R'], R_RUNtime)
            depthmode = dg.depthmode
            portcoverage = dg.maximumPortCoverageDegrees
            stbdcoverage = dg.maximumStbdCoverageDegrees
            runtimeparameters = _runtimeparametersdict(dg)
        except Exception:
            pass

    swathcoverage = None
    if portcoverage is not None and stbdcoverage is not None:
        swathcoverage = portcoverage + stbdcoverage

    # ---- significant attitude (4 x standard deviation of the per-record heave/roll/pitch) ----
    significantwaveheight = significantroll = significantpitch = None
    if attituderph:
        rph = np.asarray(attituderph, dtype=np.float64) / 100.0   # roll, pitch (deg), heave (m)
        std = np.std(rph, axis=0)
        significantroll = 4.0 * float(std[0])
        significantpitch = 4.0 * float(std[1])
        significantwaveheight = 4.0 * float(std[2])

    epsg = geodetic.epsgfromlonglat(longitude, latitude)
    info = {
        'filename': filename,
        'filesize': filesize,
        'approxlongitude': longitude,
        'approxlatitude': latitude,
        'epsg': str(epsg),
        'starttimestamp': starttimestamp,
        'endtimestamp': endtimestamp,
        'durationseconds': durationseconds,
        'firstposition': firstposition,
        'lastposition': lastposition,
        'positioncount': len(positions),
        'trackdistancemetres': trackmetres,
        'trackdistancenauticalmiles': trackmetres / 1852.0,
        'vesselspeedmps': meanspeed_mps,
        'vesselspeedknots': meanspeed_mps * 1.943844,
        'courseovergrounddegrees': course_deg,
        'approxwaterdepthm': approxdepth,
        'centrefrequencyhz': centrefrequency,
        'depthmode': depthmode,
        'portcoveragedegrees': portcoverage,
        'stbdcoveragedegrees': stbdcoverage,
        'swathcoveragedegrees': swathcoverage,
        'significantwaveheightm': significantwaveheight,
        'significantrolldegrees': significantroll,
        'significantpitchdegrees': significantpitch,
        'runtimeparameters': runtimeparameters,
        'datagramcounts': counts,
    }
    return info


###############################################################################
# record loaders - expose individual datagram types in MCP/analysis friendly form
###############################################################################

###############################################################################
def loadpositions(filename):
    '''return all position (P) records as a list of dictionaries with position, time, speed, quality and heading.'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'P':
            datagram.read()
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'latitude': datagram.latitude,
                'longitude': datagram.longitude,
                'quality': datagram.Quality,
                'speed': datagram.SpeedOverGround,
                'course': datagram.CourseOverGround,
                'heading': datagram.heading,
                'descriptor': datagram.descriptor,
            })
    r.close()
    return out

###############################################################################
def loadattitude(filename):
    '''return all attitude (A) observations as a numpy array with columns [timestamp, roll, pitch, heave, heading].'''
    r = allreader(filename)
    rows = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'A':
            datagram.read()
            for a in datagram.Attitude:
                # a = [recorddate, time(sec), status, roll, pitch, heave, heading]
                rows.append([to_timestamp(to_datetime(a[0], a[1])), a[3], a[4], a[5], a[6]])
    r.close()
    if len(rows) == 0:
        return np.empty((0, 5), dtype=float)
    return np.array(rows, dtype=float)

###############################################################################
def loadattitudefirst(filename):
    '''return one (roll, pitch, heave) sample per attitude (A) datagram as an (N, 3) numpy array (degrees, degrees, metres).

    For significant roll/pitch/heave we only need a representative time series, so this takes the
    first observation in each attitude record rather than decoding every entry.  Only the datagram
    header plus the first entry (34 bytes) is read per record - no per-entry parsing or numpy
    reshaping - and roll, pitch and heave all come out of that single read, which makes it far
    faster than loadattitude on large files.

    The A datagram layout is: header '=LBBHLLHHH' (22 bytes, numberentries at offset 20) followed by
    fixed 12-byte entries '=HHhhhH' (time, status, roll, pitch, heave, heading), each value in 0.01
    units.  In the first entry roll/pitch/heave (int16) therefore sit at byte offsets 26, 28 and 30.'''
    rph = struct.Struct('=hhh')  # roll, pitch, heave of the first entry (offset 26)
    r = allreader(filename)
    rows = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'A':
            head = r.readdatagrambytes(datagram.offset, 32)
            if len(head) >= 32 and struct.unpack_from('=H', head, 20)[0] > 0:
                rows.append(rph.unpack_from(head, 26))
    r.close()
    if len(rows) == 0:
        return np.empty((0, 3), dtype=float)
    return np.asarray(rows, dtype=np.float64) / 100.0

###############################################################################
def significantattitude(filename):
    '''estimate the significant roll, pitch and wave height (heave) from the attitude (A) time series
    using the 4 x standard deviation method:  significant value = 4 * sigma.  All three are computed
    from a single fast pass that takes one observation per attitude record (see loadattitudefirst).

    Returns a dict with significantwaveheight / significantroll / significantpitch (the 4 x sigma
    estimates, metres and degrees) plus the sample count.'''
    rph = loadattitudefirst(filename)
    n = int(rph.shape[0])
    if n == 0:
        return {'samples': 0, 'significantwaveheight': None,
                'significantroll': None, 'significantpitch': None}
    std = np.std(rph, axis=0)  # [roll, pitch, heave]
    return {
        'samples': n,
        'significantwaveheight': 4.0 * float(std[2]),
        'significantroll': 4.0 * float(std[0]),
        'significantpitch': 4.0 * float(std[1]),
    }

###############################################################################
def loadnetworkattitude(filename):
    '''return all network attitude (n) observations as a numpy array with columns
    [timestamp, roll, pitch, heave, heading].  This is the efficient way to pull the network
    attitude out of the file - it skips the per-entry raw input telegram bytes that the
    n_ATTITUDE.Attitude list carries.'''
    r = allreader(filename)
    rows = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'n':
            datagram.read()
            for a in datagram.Attitude:
                # a = [recorddate, time(sec), roll, pitch, heave, heading, telegrambytes]
                rows.append([to_timestamp(to_datetime(a[0], a[1])), a[2], a[3], a[4], a[5]])
    r.close()
    if len(rows) == 0:
        return np.empty((0, 5), dtype=float)
    return np.array(rows, dtype=float)

###############################################################################
def loadclock(filename):
    '''return all clock (C) records as a list of dictionaries.  Useful for analysing clock stability (PC time vs external time).'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'C':
            datagram.read()
            externaltimestamp = to_timestamp(to_datetime(datagram.externaldate, datagram.externaltime))
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'pcutime': datagram.time,
                'externaltime': datagram.externaltime,
                'externaltimestamp': externaltimestamp,
                'difference': datagram.time - datagram.externaltime,
                'pps': datagram.pps,
            })
    r.close()
    return out

###############################################################################
def loadheight(filename):
    '''return all height (h) records as a list of dictionaries.'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'h':
            datagram.read()
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'height': datagram.Height,
                'heighttype': datagram.HeightType,
            })
    r.close()
    return out

###############################################################################
def loadsoundvelocityprofiles(filename):
    '''return all sound velocity profile (U) datagrams as a list of dictionaries with depth and sound speed arrays.'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'U':
            datagram.read()
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'profiledate': datagram.ProfileDate,
                'profiletime': datagram.Profiletime,
                'numentries': datagram.NEntries,
                'depth': [d[0] for d in datagram.data],
                'soundspeed': [d[1] for d in datagram.data],
            })
    r.close()
    return out

###############################################################################
def loadsurfacesoundspeed(filename):
    '''return all surface sound speed (G) datagrams as a list of dictionaries.  Sound speed is in m/s.'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'G':
            datagram.read()
            speeds = [e[1] for e in datagram.soundspeed]
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'counter': datagram.counter,
                'numentries': datagram.NEntries,
                'meansoundspeed': (sum(speeds) / len(speeds)) if speeds else 0.0,
                'minsoundspeed': min(speeds) if speeds else 0.0,
                'maxsoundspeed': max(speeds) if speeds else 0.0,
                'soundspeed': datagram.soundspeed,
            })
    r.close()
    return out

###############################################################################
def loadruntimeparameters(filename):
    '''return all runtime parameter (R) records as a list of dictionaries of the decoded sonar settings.'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'R':
            datagram.read()
            out.append(_runtimeparametersdict(datagram))
    r.close()
    return out

###############################################################################
def loadtraveltime(filename, maxrecords=-1):
    '''return raw range and beam angle (N) records as a list of dictionaries.  Each record contains per-beam
    pointing angle, two way travel time, reflectivity and quality lists.  maxrecords limits the number returned.'''
    if maxrecords == -1:
        maxrecords = 999999999
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'N':
            datagram.read()
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'counter': datagram.counter,
                'soundspeedattransducer': datagram.soundspeedattransducer,
                'numtransmitsector': datagram.NumTransmitSector,
                'numreceivebeams': datagram.NumReceiveBeams,
                'numvaliddetections': datagram.NumValidDetect,
                'samplefrequency': datagram.samplefrequency,
                'beampointingangle': list(datagram.BeamPointingAngle),
                'twowaytraveltime': list(datagram.TwoWayTraveltime),
                'reflectivity': list(datagram.reflectivity),
                'qualityfactor': list(datagram.qualityfactor),
            })
            if len(out) >= maxrecords:
                break
    r.close()
    return out

###############################################################################
def loadinstallationparameters(filename):
    '''return the first installation (I) datagram parameters as a dictionary.'''
    r = allreader(filename)
    info = {}
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'I':
            datagram.read()
            info = {
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'emmodel': datagram.emmodel,
                'serialnumber': datagram.serialnumber,
                'secondaryserialnumber': datagram.Secondaryserialnumber,
                'surveylinenumber': datagram.SurveyLineNumber,
                'parameters': datagram.installationParameters,
            }
            break
    r.close()
    return info

###############################################################################
def loaddepth(filename, maxpings=-1):
    '''return per-beam soundings from the X (and D) depth datagrams as a dictionary of 1d numpy arrays:
    pingtimestamp, depth, acrosstrack, alongtrack, reflectivity, quality.  maxpings limits the number of pings.'''
    if maxpings == -1:
        maxpings = 999999999
    r = allreader(filename)
    pingts, depth, across, along, refl, qual = [], [], [], [], [], []
    pingcount = 0
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram in ('X', 'D'):
            datagram.read()
            ts = to_timestamp(to_datetime(datagram.recorddate, datagram.time))
            for i in range(datagram.nbeams):
                pingts.append(ts)
                depth.append(datagram.depth[i])
                across.append(datagram.acrosstrackdistance[i])
                along.append(datagram.alongtrackdistance[i])
                refl.append(datagram.reflectivity[i])
                qual.append(datagram.qualityfactor[i])
            pingcount += 1
            if pingcount >= maxpings:
                break
    r.close()
    return {
        'pingtimestamp': np.array(pingts, dtype=float),
        'depth': np.array(depth, dtype=float),
        'acrosstrack': np.array(across, dtype=float),
        'alongtrack': np.array(along, dtype=float),
        'reflectivity': np.array(refl, dtype=float),
        'quality': np.array(qual, dtype=float),
    }

###############################################################################
def loadseabedimage(filename, maxpings=-1):
    '''return seabed image (Y) backscatter samples as a dictionary of numpy arrays:
    pingtimestamp (per ping), numsamples (per ping) and samples (all samples concatenated, 0.1 dB).  maxpings limits the pings.'''
    if maxpings == -1:
        maxpings = 999999999
    r = allreader(filename)
    pingts, numsamples, allsamples = [], [], []
    pingcount = 0
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == 'Y':
            datagram.read()
            pingts.append(to_timestamp(to_datetime(datagram.recorddate, datagram.time)))
            numsamples.append(datagram.numSamples)
            allsamples.extend(datagram.samples)
            pingcount += 1
            if pingcount >= maxpings:
                break
    r.close()
    return {
        'pingtimestamp': np.array(pingts, dtype=float),
        'numsamples': np.array(numsamples, dtype=int),
        'samples': np.array(allsamples, dtype=float),
    }

###############################################################################
def loadpustatus(filename):
    '''return all PU status (1) records as a list of dictionaries (sensor health and last received sensor values).'''
    r = allreader(filename)
    out = []
    r.rewind()
    while r.moredata():
        typeofdatagram, datagram = r.readdatagram()
        if typeofdatagram == '1':
            datagram.read()
            out.append({
                'timestamp': to_timestamp(to_datetime(datagram.recorddate, datagram.time)),
                'counter': datagram.counter,
                'serialnumber': datagram.serialnumber,
                'pingrate': datagram.pingrate,
                'pingcounter': datagram.pingcounter,
                'ppsstatus': datagram.ppsstatus,
                'positionstatus': datagram.positionstatus,
                'attitudestatus': datagram.attitudestatus,
                'clockstatus': datagram.clockstatus,
                'headingstatus': datagram.headingstatus,
                'pustatus': datagram.pustatus,
                'lastheading': datagram.lastheading,
                'lastroll': datagram.lastroll,
                'lastpitch': datagram.lastpitch,
                'lastheave': datagram.lastheave,
                'soundspeedattransducer': datagram.soundspeedattransducer,
                'lastdepth': datagram.lastdepth,
                'alongshipvelocity': datagram.alongshipvelocity,
                'cputemperature': datagram.cputemperature,
            })
    r.close()
    return out


###############################################################################
class Cpointcloud:
    '''class to hold a point cloud'''
    # xarr = np.empty([0], dtype=float)
    # yarr = np.empty([0], dtype=float)
    # zarr = np.empty([0], dtype=float)
    # qarr = np.empty([0], dtype=float)

    # self.xarr = []
    # self.yarr = []
    # self.zarr = []
    # self.qarr = []
    # self.idarr = []

    ###############################################################################
    def __init__(self, npx=None, npy=None, npz=None, npq=None, npid=None):
        '''add the new ping of data to the existing array '''
        # np.append(self.xarr, np.array(npx))
        # np.append(self.yarr, np.array(npy))
        # np.append(self.zarr, np.array(npz))
        # np.append(self.qarr, np.array(npq))
        # np.append(self.idarr, np.array(npid))
        self.xarr = []
        self.yarr = []
        self.zarr = []
        self.qarr = []
        self.rarr = []
        # idarr = []
        # self.xarr = np.array(npx)
        # self.yarr = np.array(npy)
        # self.zarr = np.array(npz)
        # self.qarr = np.array(npq)
        # self.idarr = np.array(npid)

    ###############################################################################
    def add(self, npx, npy, npz, npq, nr=None):
        '''add the new ping of data to the existing array '''
        # self.xarr = np.append(self.xarr, np.array(npx))
        # self.yarr = np.append(self.yarr, np.array(npy))
        # self.zarr = np.append(self.zarr, np.array(npz))
        # self.qarr = np.append(self.zarr, np.array(npq))
        self.xarr.extend(npx)
        self.yarr.extend(npy)
        self.zarr.extend(npz)
        self.qarr.extend(npq)
        if nr is not None:
            self.rarr.extend(nr)


###############################################################################
class allreader:
    '''class to read a Kongsberg EM multibeam .all file'''
    allpacketheader_fmt = '=LBBHLL'
    allpacketheader_len = struct.calcsize(allpacketheader_fmt)
    allpacketheader_unpack = struct.Struct(allpacketheader_fmt).unpack_from

    def __init__(self, ALLfileName):
        if not os.path.isfile(ALLfileName):
            logging.error("file not found: %s", ALLfileName)
        self.fileName = ALLfileName
        self.fileptr = open(ALLfileName, 'rb')
        self.fileSize = os.path.getsize(ALLfileName)
        self.recorddate = ""
        self.recordtime = ""
        self.recordcounter = 0

###############################################################################
    def __str__(self):
        return pprint.pformat(vars(self))

###############################################################################
    def currentrecorddatetime(self):
        '''return a python date object from the current datagram objects raw date and time fields '''
        date_object = datetime.strptime(
            str(self.recorddate), '%Y%m%d') + timedelta(0, self.recordtime)
        return date_object

###############################################################################
    def to_datetime(self, recorddate, recordtime):
        '''return a python date object from a split date and time record'''
        date_object = datetime.strptime(
            str(recorddate), '%Y%m%d') + timedelta(0, recordtime)
        return date_object

    # def to_timestamp(self, dateObject):
    # '''return a unix timestamp from a python date object'''
    # return (dateObject - datetime(1970, 1, 1)).total_seconds()

###############################################################################
    def close(self):
        '''close the current file'''
        self.fileptr.close()

###############################################################################
    def rewind(self):
        '''go back to start of file'''
        self.fileptr.seek(0, 0)

###############################################################################
    def currentptr(self):
        '''report where we are in the file reading process'''
        return self.fileptr.tell()

###############################################################################
    def moredata(self):
        '''report how many more bytes there are to read from the file'''
        return self.fileSize - self.fileptr.tell()

###############################################################################
    def readdatagramheader(self):
        '''read the common header for any datagram'''
        try:
            curr = self.fileptr.tell()
            data = self.fileptr.read(self.allpacketheader_len)
            s = self.allpacketheader_unpack(data)

            numberofbytes = s[0]
            stx = s[1]
            typeofdatagram = chr(s[2])
            emmodel = s[3]
            recorddate = s[4]
            recordtime = float(s[5]/1000.0)
            self.recorddate = recorddate
            self.recordtime = recordtime

            # now reset file pointer
            self.fileptr.seek(curr, 0)

            # we need to add 4 bytes as the message does not contain the 4 bytes used to hold the size of the message
            # trap corrupt datagrams at the end of a file.  We see this in EM2040 systems.
            if (curr + numberofbytes + 4) > self.fileSize:
                numberofbytes = self.fileSize - curr - 4
                typeofdatagram = 'XXX'
                return numberofbytes + 4, stx, typeofdatagram, emmodel, recorddate, recordtime

            return numberofbytes + 4, stx, typeofdatagram, emmodel, recorddate, recordtime
        except struct.error:
            return 0, 0, 0, 0, 0, 0

###############################################################################
###############################################################################
    def getapproximatepositon(self):
        '''read the first position record so we have a clue where we are in the world'''
        longitude = 0
        latitude = 0
        self.rewind()
        while self.moredata():
            try:
                # logging.debug(self.fileptr.tell())
                typeofdatagram, datagram = self.readdatagram()
                if (typeofdatagram == 'P'):
                    datagram.read()
                    # trap bad values
                    if datagram.latitude < -90:
                        continue
                    if datagram.latitude > 90:
                        continue
                    if datagram.longitude < -180:
                        continue
                    if datagram.longitude > 180:
                        continue
                    longitude = datagram.longitude
                    latitude = datagram.latitude
                    break
            except:
                e = sys.exc_info()[0]
                logging.error("Error: %s.  Please check file.  it seems to be corrupt: %s" % (e, self.fileName))
        self.rewind()
        return longitude, latitude

###############################################################################
    def readdatagrambytes(self, offset, byteCount):
        '''read the entire raw bytes for the datagram without changing the file pointer.  this is used for file conditioning'''
        curr = self.fileptr.tell()
        # move the file pointer to the start of the record so we can read from disc
        self.fileptr.seek(offset, 0)
        data = self.fileptr.read(byteCount)
        self.fileptr.seek(curr, 0)
        return data

###############################################################################
    def getrecordcount(self, id=""):
        '''read through the entire file as fast as possible to get a count of all records.  useful for progress bars so user can see what is happening'''
        count = 0
        start = 0
        end = 0
        self.rewind()
        numberofbytes, stx, typeofdatagram, emmodel, recorddate, recordtime = self.readdatagramheader()
        start = to_timestamp(to_datetime(recorddate, recordtime))
        self.rewind()
        while self.moredata():
            numberofbytes, stx, typeofdatagram, emmodel, recorddate, recordtime = self.readdatagramheader()
            self.fileptr.seek(numberofbytes, 1)
            if id in typeofdatagram:
                count += 1
        self.rewind()
        end = to_timestamp(to_datetime(recorddate, recordtime))
        return count, start, end

###############################################################################
    def readdatagram(self):
        '''read the datagram header.  This permits us to skip datagrams we do not support'''
        numberofbytes, stx, typeofdatagram, emmodel, recorddate, recordtime = self.readdatagramheader()
        self.recordcounter += 1

        if typeofdatagram == '3':  # 3_EXTRA PARAMETERS DECIMAL 51
            dg = E_EXTRA(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'A':  # A ATTITUDE
            dg = A_ATTITUDE(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'C':  # C Clock
            dg = C_CLOCK(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'D':  # D depth
            dg = D_depth(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'f':  # f Raw range
            dg = f_RAWrange(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'h':  # h Height, not to be confused with H_heading!
            dg = h_HEIGHT(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'I':  # I Installation (Start)
            dg = I_INSTALLATION(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'i':  # i Installation (Stop)
            dg = I_INSTALLATION(self.fileptr, numberofbytes)
            dg.typeofdatagram = 'i'  # override with the install stop code
            return dg.typeofdatagram, dg
        if typeofdatagram == 'n':  # n ATTITUDE
            dg = n_ATTITUDE(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'N':  # N Angle and Travel time
            dg = N_TRAVELtime(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'O':  # O_qualityfactor
            dg = O_qualityfactor(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'R':  # R_RUNtime
            dg = R_RUNtime(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'P':  # P Position
            dg = P_POSITION(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'U':  # U Sound Velocity
            dg = U_SVP(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'G':  # G Surface sound speed
            dg = G_SURFACESOUNDSPEED(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == '1':  # 1 PU Status
            dg = PU_STATUS(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'X':  # X depth
            dg = X_depth(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        if typeofdatagram == 'Y':  # Y_SeabedImage
            dg = Y_SEABEDIMAGE(self.fileptr, numberofbytes)
            return dg.typeofdatagram, dg
        else:
            dg = UNKNOWN_RECORD(self.fileptr, numberofbytes, typeofdatagram)
            return dg.typeofdatagram, dg
            # self.fileptr.seek(numberofbytes, 1)
###############################################################################

    def loadInstallationRecords(self):
        '''loads all the installation into lists'''
        installStart = None
        installStop = None
        # initialMode     = None
        datagram = None
        self.rewind()
        while self.moredata():
            typeofdatagram, datagram = self.readdatagram()
            if (typeofdatagram == 'I'):
                installStart = self.readdatagrambytes(
                    datagram.offset, datagram.numberofbytes)
                datagram.read()
            if (typeofdatagram == 'i'):
                installStop = self.readdatagrambytes(
                    datagram.offset, datagram.numberofbytes)
                break
        self.rewind()
        return installStart, installStop

###############################################################################
    def loadcenterfrequency(self):
        '''determine the central frequency of the first record in the file'''
        centerfrequency = 0
        self.rewind()
        while self.moredata():
            typeofdatagram, datagram = self.readdatagram()
            if (typeofdatagram == 'N'):
                datagram.read()
                centerfrequency = datagram.centrefrequency[0]
                break
        self.rewind()
        return centerfrequency
###############################################################################

    def loaddepthmode(self):
        '''determine the central frequency of the first record in the file'''
        initialdepthmode = ""
        self.rewind()
        while self.moredata():
            typeofdatagram, datagram = self.readdatagram()
            if typeofdatagram == 'R':
                datagram.read()
                initialdepthmode = datagram.depthmode
                break
        self.rewind()
        return initialdepthmode
###############################################################################

    def loadnavigation(self, firstrecordonly=False):
        '''loads all the navigation into lists'''
        navigation = []
        selectedpositioningsystem = None
        self.rewind()
        while self.moredata():
            typeofdatagram, datagram = self.readdatagram()
            if (typeofdatagram == 'P'):
                datagram.read()
                recDate = self.currentrecorddatetime()
                if (selectedpositioningsystem == None):
                    selectedpositioningsystem = datagram.descriptor
                if (selectedpositioningsystem == datagram.descriptor):
                    # for python 2.7
                    navigation.append(
                        [to_timestamp(recDate), datagram.latitude, datagram.longitude])
                    # for python 3.4
                    # navigation.append([recDate.timestamp(), datagram.latitude, datagram.longitude])

                    if firstrecordonly:  # we only want the first record, so reset the file pointer and quit
                        self.rewind()
                        return navigation
        self.rewind()
        return navigation

###############################################################################
    def getdatagramname(self, typeofdatagram):
        '''Convert the datagram type from the code to a user readable string.  Handy for displaying to the user'''
        # Multibeam Data
        if (typeofdatagram == 'D'):
            return "D_depth"
        if (typeofdatagram == 'X'):
            return "XYZ_depth"
        if (typeofdatagram == 'K'):
            return "K_CentralBeam"
        if (typeofdatagram == 'F'):
            return "F_Rawrange"
        if (typeofdatagram == 'f'):
            return "f_Rawrange"
        if (typeofdatagram == 'N'):
            return "N_Rawrange"
        if (typeofdatagram == 'S'):
            return "S_SeabedImage"
        if (typeofdatagram == 'Y'):
            return "Y_SeabedImage"
        if (typeofdatagram == 'k'):
            return "k_WaterColumn"
        if (typeofdatagram == 'O'):
            return "O_qualityfactor"

        # ExternalSensors
        if (typeofdatagram == 'A'):
            return "A_Attitude"
        if (typeofdatagram == 'n'):
            return "network_Attitude"
        if (typeofdatagram == 'C'):
            return "C_Clock"
        if (typeofdatagram == 'h'):
            return "h_Height"
        if (typeofdatagram == 'H'):
            return "H_heading"
        if (typeofdatagram == 'P'):
            return "P_Position"
        if (typeofdatagram == 'E'):
            return "E_SingleBeam"
        if (typeofdatagram == 'T'):
            return "T_Tide"

        # SoundSpeed
        if (typeofdatagram == 'G'):
            return "G_SpeedSoundAtHead"
        if (typeofdatagram == 'U'):
            return "U_SpeedSoundProfile"
        if (typeofdatagram == 'W'):
            return "W_SpeedSOundProfileUsed"

        # Multibeam parameters
        if (typeofdatagram == 'I'):
            return "I_Installation_Start"
        if (typeofdatagram == 'i'):
            return "i_Installation_Stop"
        if (typeofdatagram == 'R'):
            return "R_Runtime"
        if (typeofdatagram == 'J'):
            return "J_TransducerTilt"
        if (typeofdatagram == '3'):
            return "3_ExtraParameters"

        # PU information and status
        if (typeofdatagram == '0'):
            return "0_PU_ID"
        if (typeofdatagram == '1'):
            return "1_PU_Status"
        if (typeofdatagram == 'B'):
            return "B_BIST_Result"


###############################################################################
class cbeam:
    __slots__ = ('sortingDirection', 'detectionInfo', 'numberOfSamplesPerBeam',
                 'centreSampleNumber', 'sector', 'takeOffAngle', 'sampleSum', 'samples')

    def __init__(self, beamDetail, angle):
        self.sortingDirection = beamDetail[0]
        self.detectionInfo = beamDetail[1]
        self.numberOfSamplesPerBeam = beamDetail[2]
        self.centreSampleNumber = beamDetail[3]
        self.sector = 0
        self.takeOffAngle = angle     # used for ARC computation
        self.sampleSum = 0         # used for backscatter ARC computation process
        self.samples = []

###############################################################################


class A_ATTITUDE_ENCODER:
    def __init__(self):
        self.data = 0

###############################################################################
    def encode(self, recordstoadd, counter):
        '''Encode a list of attitude records where the format is timestamp, roll, pitch, heave heading'''
        if (len(recordstoadd) == 0):
            return

        fulldatagram = bytearray()

        header_fmt = '=LBBHLLHHH'
        header_len = struct.calcsize(header_fmt)

        rec_fmt = "HHhhhHB"
        rec_len = struct.calcsize(rec_fmt)

        footer_fmt = '=BH'
        footer_len = struct.calcsize(footer_fmt)

        stx = 2
        typeofdatagram = 65
        model = 2045
        systemdescriptor = 0
        # set heading is ENABLED (go figure!)
        systemdescriptor = set_bit(systemdescriptor, 0)
        serialnumber = 999
        numEntries = len(recordstoadd)

        fulldatagrambytecount = header_len + \
            (rec_len*len(recordstoadd)) + footer_len
        # we need to know the first record timestamp as all observations are milliseconds from that time
        firstrecordtimestamp = float(recordstoadd[0][0])
        firstrecorddate = from_timestamp(firstrecordtimestamp)

        recorddate = int(dateToKongsbergDate(firstrecorddate))
        recordtime = int(dateToSecondsSinceMidnight(firstrecorddate)*1000)
        # we need to deduct 4 bytes as the field does not account for the 4-byte message length data which precedes the message
        try:
            header = struct.pack(header_fmt, fulldatagrambytecount-4, stx, typeofdatagram,
                                 model, recorddate, recordtime, counter, serialnumber, numEntries)
        except:
            logging.error("error encoding attitude")
            # header = struct.pack(header_fmt, fulldatagrambytecount-4, stx, typeofdatagram, model, recorddate, recordtime, counter, serialnumber, numEntries)

        fulldatagram = fulldatagram + header

        # now pack avery record from the list
        for record in recordstoadd:
            # compute the millisecond offset of the record from the first record in the datagram
            timemillisecs = round(
                (float(record[0]) - firstrecordtimestamp) * 1000)
            sensorstatus = 0
            roll = float(record[1])
            pitch = float(record[2])
            heave = float(record[3])
            heading = float(record[4])
            try:
                bodyrecord = struct.pack(rec_fmt, timemillisecs, sensorstatus, int(
                    roll*100), int(pitch*100), int(heave*100), int(heading*100), systemdescriptor)
            except:
                logging.error("error encoding attitude")
                bodyrecord = struct.pack(rec_fmt, timemillisecs, sensorstatus, int(
                    roll*100), int(pitch*100), int(heave*100), int(heading*100), systemdescriptor)
            fulldatagram = fulldatagram + bodyrecord

        # now do the footer
        # systemdescriptor = set_bit(systemdescriptor, 1) #set roll is DISABLED
        # systemdescriptor = set_bit(systemdescriptor, 2) #set pitch is DISABLED
        # systemdescriptor = set_bit(systemdescriptor, 3) #set heave is DISABLED
        # systemdescriptor = set_bit(systemdescriptor, 4) #set SENSOR as system 2
        # systemdescriptor = 30
        etx = 3
        checksum = sum(fulldatagram[5:]) % 65536
        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer

        # TEST THE CRC CODE pkpk
        # c = CRC16()
        # chk = c.calculate(fulldatagram)

        return fulldatagram

###############################################################################


class A_ATTITUDE:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'A'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        # read the whole datagram once and parse it from the buffer (entries are fixed size).
        raw = self.fileptr.read(self.numberofbytes)

        hdr = struct.Struct('=LBBHLLHHH')
        s = hdr.unpack_from(raw, 0)

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.numberentries = s[8]

        # decode all fixed-size entries in one pass with iter_unpack (was a per-entry file read)
        entry = struct.Struct('=HHhhhH')
        pos = hdr.size
        recorddate = self.recorddate
        basetime = self.time
        # entry = time, status, roll, pitch, heave, heading
        self.Attitude = [[recorddate, basetime + e[0]/1000.0, e[1],
                          e[2]/100.0, e[3]/100.0, e[4]/100.0, e[5]/100.0]
                         for e in entry.iter_unpack(raw[pos:pos + entry.size * self.numberentries])]
        pos += entry.size * self.numberentries

        s = struct.unpack_from('=BBH', raw, pos)
        self.systemdescriptor = s[0]
        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
class C_CLOCK:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'C'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHLLBBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        # bytesRead = rec_len
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5] / 1000.0)
        self.clockcounter = s[6]
        self.serialnumber = s[7]
        self.externaldate = s[8]
        self.externaltime = s[9] / 1000.0
        self.pps = s[10]
        self.etx = s[11]
        self.checksum = s[12]

    def __str__(self):
        if self.pps == 0:
            ppsInUse = "pps NOT in use"
        else:
            ppsInUse = "pps in use"

        s = '%d,%d,%.3f,%.3f,%.3f,%s' % (self.recorddate, self.externaldate,
                                         self.time, self.externaltime, self.time - self.externaltime, ppsInUse)
        return s

###############################################################################
class D_depth:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'D'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHHHHBBBBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.heading = float(s[8] / float(100))
        self.soundspeedattransducer = float(s[9] / float(10))
        self.transducerdepth = float(s[10] / float(100))
        self.maxbeams = s[11]
        self.nbeams = s[12]
        self.zresolution = float(s[13] / float(100))
        self.xyresolution = float(s[14] / float(100))
        self.samplefrequency = s[15]

        # decode every beam in a single block read + iter_unpack, then split into per-field
        # columns with one C-level zip (replaces the original per-beam read/unpack hot loop).
        if self.emmodel < 700:
            beam_struct = struct.Struct('=H3h2H2BbB')
        else:
            beam_struct = struct.Struct('=4h2H2BbB')
        beamdata = self.fileptr.read(beam_struct.size * self.nbeams)
        columns = list(zip(*beam_struct.iter_unpack(beamdata))) or [()] * 10

        # depth, acrosstrack and alongtrack keep the original NaN guard (NaN != NaN)
        self.depth = [0.0 if x != x else x for x in (v / 100.0 for v in columns[0])]
        self.acrosstrackdistance = [0.0 if x != x else x for x in (v / 100.0 for v in columns[1])]
        self.alongtrackdistance = [0.0 if x != x else x for x in (v / 100.0 for v in columns[2])]
        self.beamdepressionangle = [v / 100.0 for v in columns[3]]
        self.beamazmuthangle = [v / 100.0 for v in columns[4]]
        self.range = [v / 100.0 for v in columns[5]]
        self.qualityfactor = list(columns[6])
        self.lengthofdetectionwindow = list(columns[7])
        self.reflectivity = [v / 100.0 for v in columns[8]]
        self.beamnumber = list(columns[9])

        rec_fmt = '=bBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

        self.rangemultiplier = s[0]
        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
    def encode(self):
        '''Encode a depth D datagram record'''
        header_fmt = '=LBBHLLHHHHHBBBBH'
        header_len = struct.calcsize(header_fmt)

        fulldatagram = bytearray()

        # now read the variable part of the Record
        if self.emmodel < 700:
            rec_fmt = '=H3h2H2BbB'
        else:
            rec_fmt = '=4h2H2BbB'
        rec_len = struct.calcsize(rec_fmt)

        footer_fmt = '=BBH'
        footer_len = struct.calcsize(footer_fmt)

        fulldatagrambytecount = header_len + (rec_len*self.nbeams) + footer_len

        # pack the header
        recordtime = int(dateToSecondsSinceMidnight(
            from_timestamp(self.time))*1000)
        header = struct.pack(header_fmt,
                             fulldatagrambytecount-4,
                             self.stx,
                             ord(self.typeofdatagram),
                             self.emmodel,
                             self.recorddate,
                             recordtime,
                             int(self.counter),
                             int(self.serialnumber),
                             int(self.heading * 100),
                             int(self.soundspeedattransducer * 10),
                             int(self.transducerdepth * 100),
                             int(self.maxbeams),
                             int(self.nbeams),
                             int(self.zresolution * 100),
                             int(self.xyresolution * 100),
                             int(self.samplefrequency))
        fulldatagram = fulldatagram + header
        header_fmt = '=LBBHLLHHHHHBBBBH'

        # pack the beam summary info
        for i in range(self.nbeams):
            bodyrecord = struct.pack(rec_fmt,
                                     int(self.depth[i] * 100),
                                     int(self.acrosstrackdistance[i] * 100),
                                     int(self.alongtrackdistance[i] * 100),
                                     int(self.beamdepressionangle[i] * 100),
                                     int(self.beamazmuthangle[i] * 100),
                                     int(self.range[i] * 100),
                                     self.qualityfactor[i],
                                     self.lengthofdetectionwindow[i],
                                     int(self.reflectivity[i] * 100),
                                     self.beamnumber[i])
            fulldatagram = fulldatagram + bodyrecord

        tmp = struct.pack('=b', self.rangemultiplier)
        fulldatagram = fulldatagram + tmp

        # now pack the footer
        # systemdescriptor = 1
        etx = 3
        checksum = sum(fulldatagram[5:]) % 65536
        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer

        return fulldatagram

###############################################################################
class E_EXTRA:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = '3'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.ExtraData = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.contentidentifier = s[8]

        # now read the variable position part of the Record
        if self.numberofbytes % 2 != 0:
            bytesToRead = self.numberofbytes - rec_len - 5  # 'sBBH'
        else:
            bytesToRead = self.numberofbytes - rec_len - 4  # 'sBH'

        # now read the block of data whatever it may contain
        self.data = self.fileptr.read(bytesToRead)

        # # now spare byte only if necessary
        # if self.numberofbytes % 2 != 0:
        # self.fileptr.read(1)

        # read an empty byte
        self.fileptr.read(1)

        # now read the footer
        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)

###############################################################################
class f_RAWrange:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'f'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHH HHLl4H'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        bytesRead = rec_len
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.pingcounter = s[6]
        self.serialnumber = s[7]

        self.NumTransmitSector = s[8]
        self.NumReceiveBeams = s[9]
        self.samplefrequency = float(s[10] / 100)
        self.ROVdepth = s[11]
        self.soundspeedattransducer = s[12] / 10
        self.maxbeams = s[13]
        self.Spare1 = s[14]
        self.Spare2 = s[15]

        self.TiltAngle = [0 for i in range(self.NumTransmitSector)]
        self.Focusrange = [0 for i in range(self.NumTransmitSector)]
        self.SignalLength = [0 for i in range(self.NumTransmitSector)]
        self.SectorTransmitDelay = [0 for i in range(self.NumTransmitSector)]
        self.centrefrequency = [0 for i in range(self.NumTransmitSector)]
        self.MeanAbsorption = [0 for i in range(self.NumTransmitSector)]
        self.SignalWaveformID = [0 for i in range(self.NumTransmitSector)]
        self.TransmitSectorNumberTX = [
            0 for i in range(self.NumTransmitSector)]
        self.SignalBandwidth = [0 for i in range(self.NumTransmitSector)]

        # # now read the variable part of the Transmit Record
        rec_fmt = '=hHLLLHBB'
        tx_struct = struct.Struct(rec_fmt)
        txdata = self.fileptr.read(tx_struct.size * self.NumTransmitSector)
        bytesRead += tx_struct.size * self.NumTransmitSector
        txcols = list(zip(*tx_struct.iter_unpack(txdata))) or [()] * 8
        self.TiltAngle = [v / 100.0 for v in txcols[0]]
        self.Focusrange = [v / 10 for v in txcols[1]]
        self.SignalLength = list(txcols[2])
        self.SectorTransmitDelay = list(txcols[3])
        self.centrefrequency = list(txcols[4])
        self.SignalBandwidth = list(txcols[5])
        self.SignalWaveformID = list(txcols[6])
        self.TransmitSectorNumberTX = list(txcols[7])

        # now read the receive record - one block read + zip transpose (was a per-beam loop)
        rx_struct = struct.Struct('=hHBbBBhH')
        rxdata = self.fileptr.read(rx_struct.size * self.NumReceiveBeams)
        bytesRead += rx_struct.size * self.NumReceiveBeams
        rxcols = list(zip(*rx_struct.iter_unpack(rxdata))) or [()] * 8
        sf4 = 4 * self.samplefrequency
        self.BeamPointingAngle = [v / 100.0 for v in rxcols[0]]
        self.TwoWayTraveltime = [v / sf4 for v in rxcols[1]]
        self.TransmitSectorNumber = list(rxcols[2])
        self.reflectivity = [v / 2.0 for v in rxcols[3]]
        self.qualityfactor = list(rxcols[4])
        self.DetectionWindow = list(rxcols[5])
        self.beamnumber = list(rxcols[6])

        rec_fmt = '=BBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
    def encode(self):
        '''Encode a depth f datagram record'''
        systemdescriptor = 1

        header_fmt = '=LBBHLLHH HHLl4H'
        header_len = struct.calcsize(header_fmt)

        fulldatagram = bytearray()

        # # now read the variable part of the Transmit Record
        rec_fmt = '=hHLLLHBB'
        rec_len = struct.calcsize(rec_fmt)

        # now read the variable part of the recieve record
        rx_rec_fmt = '=hHBbBBhHB'
        rx_rec_len = struct.calcsize(rx_rec_fmt)

        footer_fmt = '=BH'
        footer_len = struct.calcsize(footer_fmt)

        fulldatagrambytecount = header_len + \
            (rec_len*self.NumTransmitSector) + \
            (rx_rec_len*self.NumReceiveBeams) + footer_len

        # pack the header
        recordtime = int(dateToSecondsSinceMidnight(
            from_timestamp(self.time))*1000)
        header = struct.pack(header_fmt,
                             fulldatagrambytecount-4,
                             self.stx,
                             ord(self.typeofdatagram),
                             self.emmodel,
                             self.recorddate,
                             recordtime,
                             self.pingcounter,
                             self.serialnumber,
                             self.NumTransmitSector,
                             self.NumReceiveBeams,
                             int(self.samplefrequency * 100),
                             self.ROVdepth,
                             int(self.soundspeedattransducer * 10),
                             self.maxbeams,
                             self.Spare1,
                             self.Spare2)
        fulldatagram = fulldatagram + header

        for i in range(self.NumTransmitSector):
            sectorRecord = struct.pack(rec_fmt,
                                       int(self.TiltAngle[i] * 100),
                                       int(self.Focusrange[i] * 10),
                                       self.SignalLength[i],
                                       self.SectorTransmitDelay[i],
                                       self.centrefrequency[i],
                                       self.SignalBandwidth[i],
                                       self.SignalWaveformID[i],
                                       self.TransmitSectorNumberTX[i])
            fulldatagram = fulldatagram + sectorRecord

        # pack the beam summary info
        for i in range(self.NumReceiveBeams):
            bodyrecord = struct.pack(rx_rec_fmt,
                                     int(self.BeamPointingAngle[i] * 100.0),
                                     int(self.TwoWayTraveltime[i]
                                         * (4 * self.samplefrequency)),
                                     self.TransmitSectorNumber[i],
                                     int(self.reflectivity[i] * 2.0),
                                     self.qualityfactor[i],
                                     self.DetectionWindow[i],
                                     self.beamnumber[i],
                                     self.Spare1,
                                     systemdescriptor)
            fulldatagram = fulldatagram + bodyrecord

        # now pack the footer
        etx = 3
        checksum = sum(fulldatagram[5:]) % 65536
        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer

        return fulldatagram

###############################################################################
class h_HEIGHT:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'h'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""
        self.Height = 0
        self.HeightType = 0

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHlB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.Height = float(s[8] / float(100))
        self.HeightType = s[9]

        # now read the footer
        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)

##############################################################################
class h_HEIGHT_ENCODER:
    def __init__(self):
        self.data = 0

###############################################################################
    def encode(self, height, recorddate, recordtime, counter):
        '''Encode a Height datagram record'''
        rec_fmt = '=LBBHLLHHlB'
        rec_len = struct.calcsize(rec_fmt)
        # 0 = the height of the waterline at the vertical datum (from KM datagram manual)
        heightType = 0
        serialnumber = 999
        stx = 2
        typeofdatagram = 'h'
        checksum = 0
        model = 2045  # needs to be a sensible value to record is valid.  Maybe would be better to pass this from above
        try:
            fulldatagram = struct.pack(rec_fmt, rec_len-4, stx, ord(typeofdatagram), model, int(
                recorddate), int(recordtime), counter, serialnumber, int(height * 100), int(heightType))
            etx = 3
            checksum = sum(fulldatagram[5:]) % 65536
            footer = struct.pack('=BH', etx, checksum)
            fulldatagram = fulldatagram + footer
        except:
            logging.error("error encoding height field")
            # header = struct.pack(rec_fmt, rec_len-4, stx, ord(typeofdatagram), model, int(recorddate), int(recordtime), counter, serialnumber, int(height * 100), int(heightType), etx, checksum)
        return fulldatagram

###############################################################################
class I_INSTALLATION:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'I'  # assign the KM code for this datagram type
        # remember where this packet resides in the file so we can return if needed
        self.offset = fileptr.tell()
        # remember how many bytes this packet contains. This includes the first 4 bytes represnting the number of bytes inthe datagram
        self.numberofbytes = numberofbytes
        # remember the file pointer so we do not need to pass from the host process
        self.fileptr = fileptr
        # move the file pointer to the end of the record so we can skip as the default actions
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""

###############################################################################
    def read(self):
        # move the file pointer to the start of the record so we can read from disc
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLL3H'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        # read the record from disc
        bytesRead = rec_len
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.SurveyLineNumber = s[6]
        self.serialnumber = s[7]
        self.Secondaryserialnumber = s[8]

        # we do not need to read the header twice
        totalAsciiBytes = self.numberofbytes - rec_len
        data = self.fileptr.read(totalAsciiBytes)  # read the record from disc
        bytesRead = bytesRead + totalAsciiBytes
        parameters = data.decode('utf-8', errors="ignore").split(",")
        self.installationParameters = {}
        for p in parameters:
            parts = p.split("=")
            # logging.debug(parts)
            if len(parts) > 1:
                self.installationParameters[parts[0]] = parts[1].strip()

        # read any trailing bytes.  We have seen the need for this with some .all files.
        if bytesRead < self.numberofbytes:
            self.fileptr.read(int(self.numberofbytes - bytesRead))

###############################################################################
class n_ATTITUDE:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'n'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        # read the whole datagram once and parse it from the buffer.  each attitude entry is a
        # fixed 12-byte header followed by a variable length input telegram, so unpack_from with a
        # running offset replaces the original two file reads per entry.
        raw = self.fileptr.read(self.numberofbytes)

        hdr = struct.Struct('=LBBHLLHHHbB')
        s = hdr.unpack_from(raw, 0)

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.numberentries = s[8]
        self.Systemdescriptor = s[9]

        entry = struct.Struct('=HhhhHB')
        entry_unpack = entry.unpack_from
        entry_size = entry.size
        pos = hdr.size
        recorddate = self.recorddate
        basetime = self.time

        attitude = []
        for _ in range(self.numberentries):
            e = entry_unpack(raw, pos)
            pos += entry_size
            inputTelegramSize = e[5]
            data = raw[pos:pos + inputTelegramSize]
            pos += inputTelegramSize
            # entry layout: [recorddate, time, roll, pitch, heave, heading, input-telegram bytes]
            # roll/pitch/heave/heading are all in 0.01 units (e[5] is the telegram byte count, not a
            # sensor value, so it is used to slice the telegram above rather than stored as data).
            attitude.append([recorddate, basetime + e[0]/1000,
                             e[1]/100.0, e[2]/100.0, e[3]/100.0, e[4]/100.0, data])
        self.Attitude = attitude

        # footer layout at the end of the record: [spare byte][etx][checksum]
        self.etx, self.checksum = struct.unpack_from('=BH', raw, len(raw) - 3)

###############################################################################
class N_TRAVELtime:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'N'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHHHHHfL'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        bytesRead = rec_len
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.soundspeedattransducer = s[8]
        self.NumTransmitSector = s[9]
        self.NumReceiveBeams = s[10]
        self.NumValidDetect = s[11]
        self.samplefrequency = float(s[12])
        self.DScale = s[13]

        self.TiltAngle = [0 for i in range(self.NumTransmitSector)]
        self.Focusrange = [0 for i in range(self.NumTransmitSector)]
        self.SignalLength = [0 for i in range(self.NumTransmitSector)]
        self.SectorTransmitDelay = [0 for i in range(self.NumTransmitSector)]
        self.centrefrequency = [0 for i in range(self.NumTransmitSector)]
        self.MeanAbsorption = [0 for i in range(self.NumTransmitSector)]
        self.SignalWaveformID = [0 for i in range(self.NumTransmitSector)]
        self.TransmitSectorNumberTX = [
            0 for i in range(self.NumTransmitSector)]
        self.SignalBandwidth = [0 for i in range(self.NumTransmitSector)]

        # # now read the variable part of the Transmit Record
        rec_fmt = '=hHfffHBBf'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        for i in range(self.NumTransmitSector):
            data = self.fileptr.read(rec_len)
            bytesRead += rec_len
            s = rec_unpack(data)
            self.TiltAngle[i] = float(s[0]) / float(100)
            self.Focusrange[i] = s[1]
            self.SignalLength[i] = float(s[2])
            self.SectorTransmitDelay[i] = float(s[3])
            self.centrefrequency[i] = float(s[4])
            self.MeanAbsorption[i] = s[5]
            self.SignalWaveformID[i] = s[6]
            self.TransmitSectorNumberTX[i] = s[7]
            self.SignalBandwidth[i] = float(s[8])

        # now read the receive record - one block read + zip transpose (was a per-beam loop)
        rx_struct = struct.Struct('=hBBHBbfhbB')
        rxdata = self.fileptr.read(rx_struct.size * self.NumReceiveBeams)
        bytesRead += rx_struct.size * self.NumReceiveBeams
        rxcols = list(zip(*rx_struct.iter_unpack(rxdata))) or [()] * 10
        self.BeamPointingAngle = [v / 100.0 for v in rxcols[0]]
        self.TransmitSectorNumber = list(rxcols[1])
        self.DetectionInfo = list(rxcols[2])
        self.DetectionWindow = list(rxcols[3])
        self.qualityfactor = list(rxcols[4])
        self.DCorr = list(rxcols[5])
        self.TwoWayTraveltime = list(rxcols[6])
        self.reflectivity = list(rxcols[7])
        self.realtimecleaninginformation = list(rxcols[8])
        self.Spare = list(rxcols[9])

        rec_fmt = '=BBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
class O_qualityfactor:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'O'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.data = ""
        self.fileptr.seek(numberofbytes, 1)

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHHBB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.nbeams = s[8]
        self.NParPerBeam = s[9]
        self.Spare = s[10]

        self.qualityfactor = [0 for i in range(self.nbeams)]

        rec_fmt = '=' + str(self.NParPerBeam) + 'f'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        i = 0
        while i < self.nbeams:
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            self.qualityfactor[i] = float(s[0])
            i = i + 1

        rec_fmt = '=bBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

        self.rangemultiplier = s[0]
        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
    def encode(self):
        '''Encode an O_qualityfactor datagram record'''
        header_fmt = '=LBBHLLHHHBB'
        header_len = struct.calcsize(header_fmt)

        fulldatagram = bytearray()

        # now read the variable part of the Record
        rec_fmt = '=' + str(self.NParPerBeam) + 'f'
        rec_len = struct.calcsize(rec_fmt)
        # rec_unpack = struct.Struct(rec_fmt).unpack

        footer_fmt = '=BBH'
        footer_len = struct.calcsize(footer_fmt)

        fulldatagrambytecount = header_len + \
            (rec_len*self.nbeams * self.NParPerBeam) + footer_len

        # pack the header
        recordtime = int(dateToSecondsSinceMidnight(
            from_timestamp(self.time))*1000)
        header = struct.pack(header_fmt,
                             fulldatagrambytecount-4,
                             self.stx,
                             ord(self.typeofdatagram),
                             self.emmodel,
                             self.recorddate,
                             recordtime,
                             int(self.counter),
                             int(self.serialnumber),
                             int(self.nbeams),
                             int(self.NParPerBeam),
                             int(self.Spare))
        fulldatagram = fulldatagram + header

        # pack the beam summary info
        for i in range(self.nbeams):
            # for j in range (self.NParPerBeam):
            bodyrecord = struct.pack(rec_fmt,
                                     float(self.qualityfactor[i]))  # for now pack the same value.  If we see any .all files with more than 1, we can test and fix this. pkpk
            fulldatagram = fulldatagram + bodyrecord

        # now pack the footer
        # systemdescriptor = 1
        etx = 3
        checksum = sum(fulldatagram[5:]) % 65536
        footer = struct.pack(footer_fmt, 0, etx, checksum)
        fulldatagram = fulldatagram + footer

        return fulldatagram


###############################################################################
class P_POSITION:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'P'  # assign the KM code for this datagram type
        # remember where this packet resides in the file so we can return if needed
        self.offset = fileptr.tell()
        # remember how many bytes this packet contains
        self.numberofbytes = numberofbytes
        # remember the file pointer so we do not need to pass from the host process
        self.fileptr = fileptr
        # move the file pointer to the end of the record so we can skip as the default actions
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""

###############################################################################
    def read(self):
        # move the file pointer to the start of the record so we can read from disc
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHll4HBB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        # bytesRead = rec_len
        s = rec_unpack(self.fileptr.read(rec_len))

        self.numberofbytes = s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.latitude = float(s[8] / float(20000000))
        self.longitude = float(s[9] / float(10000000))
        self.Quality = float(s[10] / float(100))
        self.SpeedOverGround = float(s[11] / float(100))
        self.CourseOverGround = float(s[12] / float(100))
        self.heading = float(s[13] / float(100))
        self.descriptor = s[14]
        self.NBytesDatagram = s[15]

        # now spare byte only if necessary
        if (rec_len + self.NBytesDatagram + 3) % 2 != 0:
            self.NBytesDatagram += 1

        # now read the block of data whatever it may contain
        self.data = self.fileptr.read(self.NBytesDatagram)

        # # now spare byte only if necessary
        # if (rec_len + self.NBytesDatagram + 3) % 2 != 0:
        #     self.fileptr.read(1)

        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)


###############################################################################
def readfooter(numberofbytes, fileptr):
    rec_fmt = '=BH'

    rec_len = struct.calcsize(rec_fmt)
    rec_unpack = struct.Struct(rec_fmt).unpack_from
    s = rec_unpack(fileptr.read(rec_len))
    etx = s[0]
    checksum = s[1]
    # self.DatagramAsReceived = s[0].decode('utf-8').rstrip('\x00')
    # if numberofbytes % 2 == 0:
    # # skip the spare byte
    # etx                = s[2]
    # checksum        = s[3]
    # else:
    # etx                = s[1]
    # checksum        = s[2]

    # #read any trailing bytes.  We have seen the need for this with some .all files.
    # if bytesRead < self.numberofbytes:
    # self.fileptr.read(int(self.numberofbytes - bytesRead))

    return etx, checksum

##############################################################################
class P_POSITION_ENCODER:
    def __init__(self):
        self.data = 0

###############################################################################
    def encode(self, recorddate, recordtime, counter, latitude, longitude, quality, speedOverGround, courseOverGround, heading, descriptor, nBytesDatagram, data):
        '''Encode a Position datagram record'''
        rec_fmt = '=LBBHLLHHll4HBB'

        rec_len = struct.calcsize(rec_fmt)
        # heightType = 0 #0 = the height of the waterline at the vertical datum (from KM datagram manual)
        serialnumber = 999
        stx = 2
        typeofdatagram = 'P'
        checksum = 0
        model = 2045  # needs to be a sensible value to record is valid.  Maybe would be better to pass this from above
        data = ""  # for now dont write out the raw position string.  I am not sure if this helps or not.  It can be included if we feel it adds value over confusion
        # try:
        # fulldatagram = struct.pack(rec_fmt, rec_len-4, stx, ord(typeofdatagram), model, int(recorddate), int(recordtime), counter, serialnumber, int(height * 100), int(heightType))
        # remove 4 bytes from header and add 3 more for footer
        recordLength = rec_len - 4 + len(data) + 3
        fulldatagram = struct.pack(rec_fmt, recordLength,
                                   stx,
                                   ord(typeofdatagram),
                                   model,
                                   int(recorddate),
                                   int(recordtime),
                                   int(counter),
                                   int(serialnumber),
                                   int(latitude * float(20000000)),
                                   int(longitude * float(10000000)),
                                   int(quality * 100),
                                   int(speedOverGround * float(100)),
                                   int(courseOverGround * float(100)),
                                   int(heading * float(100)),
                                   int(descriptor),
                                   int(len(data)))
        # now add the raw bytes, typically NMEA GGA string
        fulldatagram = fulldatagram + data.encode('ascii')
        etx = 3
        checksum = sum(fulldatagram[5:]) % 65536
        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer
        return fulldatagram
        # except:
        # logging.error("error encoding POSITION Record")
        # return

###############################################################################
class R_RUNtime:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'R'  # assign the KM code for this datagram type
        # remember where this packet resides in the file so we can return if needed
        self.offset = fileptr.tell()
        # remember how many bytes this packet contains
        self.numberofbytes = numberofbytes
        # remember the file pointer so we do not need to pass from the host process
        self.fileptr = fileptr
        # move the file pointer to the end of the record so we can skip as the default actions
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""

###############################################################################
    def read(self):
        # move the file pointer to the start of the record so we can read from disc
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHBBBBBBHHHHHbBBBBBHBBBBHHBBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = s[5]/1000
        self.counter = s[6]
        self.serialnumber = s[7]

        self.operatorStationStatus = s[8]
        self.processingUnitStatus = s[9]
        self.BSPStatus = s[10]
        self.sonarHeadStatus = s[11]
        self.mode = s[12]
        self.filterIdentifier = s[13]
        self.minimumdepth = s[14]
        self.maximumdepth = s[15]
        self.absorptionCoefficient = s[16]/100
        self.transmitPulseLength = s[17]
        self.transmitBeamWidth = s[18]
        self.transmitPower = s[19]
        self.receiveBeamWidth = s[20]
        self.receiveBandwidth = s[21]
        self.mode2 = s[22]
        self.tvg = s[23]
        self.sourceOfSpeedSound = s[24]
        self.maximumPortWidth = s[25]
        self.beamSpacing = s[26]
        self.maximumPortCoverageDegrees = s[27]
        self.yawMode = s[28]
        # self.yawAndPitchStabilisationMode= s[28]
        self.maximumStbdCoverageDegrees = s[29]
        self.maximumStbdWidth = s[30]
        self.transmitAAlongTilt = s[31]
        self.filterIdentifier2 = s[32]
        self.etx = s[33]
        self.checksum = s[34]

        self.beamSpacingString = "Determined by beamwidth"
        if (isBitSet(self.beamSpacing, 0)):
            self.beamSpacingString = "Equidistant"
        if (isBitSet(self.beamSpacing, 1)):
            self.beamSpacingString = "Equiangular"
        if (isBitSet(self.beamSpacing, 0) and isBitSet(self.beamSpacing, 1)):
            self.beamSpacingString = "High density equidistant"
        if (isBitSet(self.beamSpacing, 7)):
            self.beamSpacingString = self.beamSpacingString + "+Two Heads"

        self.yawAndPitchStabilisationMode = "Yaw stabilised OFF"
        if (isBitSet(self.yawMode, 0)):
            self.yawAndPitchStabilisationMode = "Yaw stabilised ON"
        if (isBitSet(self.yawMode, 1)):
            self.yawAndPitchStabilisationMode = "Yaw stabilised ON"
        if (isBitSet(self.yawMode, 1) and isBitSet(self.yawMode, 0)):
            self.yawAndPitchStabilisationMode = "Yaw stabilised ON (manual)"
        if (isBitSet(self.yawMode, 7)):
            self.yawAndPitchStabilisationMode = self.yawAndPitchStabilisationMode + \
                "+Pitch stabilised ON"

        self.depthmode = "VeryShallow"
        if (isBitSet(self.mode, 0)):
            self.depthmode = "Shallow"
        if (isBitSet(self.mode, 1)):
            self.depthmode = "Medium"
        if (isBitSet(self.mode, 0) & (isBitSet(self.mode, 1))):
            self.depthmode = "VeryDeep"
        if (isBitSet(self.mode, 2)):
            self.depthmode = "VeryDeep"
        if (isBitSet(self.mode, 0) & (isBitSet(self.mode, 2))):
            self.depthmode = "VeryDeep"

        if str(self.emmodel) in 'EM2040, EM2045':
            self.depthmode = "200kHz"
            if (isBitSet(self.mode, 0)):
                self.depthmode = "300kHz"
            if (isBitSet(self.mode, 1)):
                self.depthmode = "400kHz"

        self.TXPulseForm = "CW"
        if (isBitSet(self.mode, 4)):
            self.TXPulseForm = "Mixed"
        if (isBitSet(self.mode, 5)):
            self.TXPulseForm = "FM"

        self.dualSwathMode = "Off"
        if (isBitSet(self.mode, 6)):
            self.dualSwathMode = "Fixed"
        if (isBitSet(self.mode, 7)):
            self.dualSwathMode = "Dynamic"

        self.filterSetting = "SpikeFilterOff"
        if (isBitSet(self.filterIdentifier, 0)):
            self.filterSetting = "SpikeFilterWeak"
        if (isBitSet(self.filterIdentifier, 1)):
            self.filterSetting = "SpikeFilterMedium"
        if (isBitSet(self.filterIdentifier, 0) & (isBitSet(self.filterIdentifier, 1))):
            self.filterSetting = "SpikeFilterMedium"
        if (isBitSet(self.filterIdentifier, 2)):
            self.filterSetting += "+SlopeOn"
        if (isBitSet(self.filterIdentifier, 3)):
            self.filterSetting += "+SectorTrackingOn"
        if ((not isBitSet(self.filterIdentifier, 4)) & (not isBitSet(self.filterIdentifier, 7))):
            self.filterSetting += "+rangeGatesNormal"
        if ((isBitSet(self.filterIdentifier, 4)) & (not isBitSet(self.filterIdentifier, 7))):
            self.filterSetting += "+rangeGatesLarge"
        if ((not isBitSet(self.filterIdentifier, 4)) & (isBitSet(self.filterIdentifier, 7))):
            self.filterSetting += "+rangeGatesSmall"
        if (isBitSet(self.filterIdentifier, 5)):
            self.filterSetting += "+AerationFilterOn"
        if (isBitSet(self.filterIdentifier, 6)):
            self.filterSetting += "+InterferenceFilterOn"

###############################################################################
    def header(self):
        header = ""
        header += "typeofdatagram,"
        header += "emmodel,"
        header += "recorddate,"
        header += "time,"
        header += "counter,"
        header += "serialnumber,"
        header += "operatorStationStatus,"
        header += "processingUnitStatus,"
        header += "BSPStatus,"
        header += "sonarHeadStatus,"
        header += "mode,"
        header += "dualSwathMode,"
        header += "TXPulseForm,"
        header += "filterIdentifier,"
        header += "filterSetting,"
        header += "minimumdepth,"
        header += "maximumdepth,"
        header += "absorptionCoefficient,"
        header += "transmitPulseLength,"
        header += "transmitBeamWidth,"
        header += "transmitPower,"
        header += "receiveBeamWidth,"
        header += "receiveBandwidth,"
        header += "mode2,"
        header += "tvg,"
        header += "sourceOfSpeedSound,"
        header += "maximumPortWidth,"
        header += "beamSpacing,"
        header += "maximumPortCoverageDegrees,"
        header += "yawMode,"
        header += "yawAndPitchStabilisationMode,"
        header += "maximumStbdCoverageDegrees,"
        header += "maximumStbdWidth,"
        header += "transmitAAlongTilt,"
        header += "filterIdentifier2,"
        return header

###############################################################################
    def parameters(self):
        '''this function returns the runtime record in a human readmable format.  there are 2 strings returned, teh header which changes with every record and the paramters which only change when the user changes a setting.  this means we can reduce duplicate records by testing the parameters string for changes'''
        s = '%s,%d,' % (self.operatorStationStatus, self.processingUnitStatus)
        s += '%d,%d,' % (self.BSPStatus, self.sonarHeadStatus)
        s += '%d,%s,%s,%d,%s,' % (self.mode, self.dualSwathMode,
                                  self.TXPulseForm, self.filterIdentifier, self.filterSetting)
        s += '%.3f,%.3f,' % (self.minimumdepth, self.maximumdepth)
        s += '%.3f,%.3f,' % (self.absorptionCoefficient,
                             self.transmitPulseLength)
        s += '%.3f,%.3f,' % (self.transmitBeamWidth, self.transmitPower)
        s += '%.3f,%.3f,' % (self.receiveBeamWidth, self.receiveBandwidth)
        s += '%d,%.3f,' % (self.mode2, self.tvg)
        s += '%d,%d,' % (self.sourceOfSpeedSound, self.maximumPortWidth)
        s += '%.3f,%d,' % (self.beamSpacing, self.maximumPortCoverageDegrees)
        s += '%s,%s,%d,' % (self.yawMode, self.yawAndPitchStabilisationMode,
                            self.maximumStbdCoverageDegrees)
        s += '%d,%d,' % (self.maximumStbdWidth, self.transmitAAlongTilt)
        s += '%s' % (self.filterIdentifier2)
        return s

    def __str__(self):
        '''this function returns the runtime record in a human readmable format.  there are 2 strings returned, teh header which changes with every record and the paramters which only change when the user changes a setting.  this means we can reduce duplicate records by testing the parameters string for changes'''
        s = '%s,%d,' % (self.typeofdatagram, self.emmodel)
        s += '%s,%.3f,' % (self.recorddate, self.time)
        s += '%d,%d,' % (self.counter, self.serialnumber)
        s += '%s,%d,' % (self.operatorStationStatus, self.processingUnitStatus)
        s += '%d,%d,' % (self.BSPStatus, self.sonarHeadStatus)
        s += '%d,%s,%s,%d,%s,' % (self.mode, self.dualSwathMode,
                                  self.TXPulseForm, self.filterIdentifier, self.filterSetting)
        s += '%.3f,%.3f,' % (self.minimumdepth, self.maximumdepth)
        s += '%.3f,%.3f,' % (self.absorptionCoefficient,
                             self.transmitPulseLength)
        s += '%.3f,%.3f,' % (self.transmitBeamWidth, self.transmitPower)
        s += '%.3f,%.3f,' % (self.receiveBeamWidth, self.receiveBandwidth)
        s += '%d,%.3f,' % (self.mode2, self.tvg)
        s += '%d,%d,' % (self.sourceOfSpeedSound, self.maximumPortWidth)
        s += '%.3f,%d,' % (self.beamSpacing, self.maximumPortCoverageDegrees)
        s += '%s,%s,%d,' % (self.yawMode, self.yawAndPitchStabilisationMode,
                            self.maximumStbdCoverageDegrees)
        s += '%d,%d,' % (self.maximumStbdWidth, self.transmitAAlongTilt)
        s += '%s' % (self.filterIdentifier2)
        return s

        # return pprint.pformat(vars(self))

###############################################################################
class UNKNOWN_RECORD:
    '''used as a convenience tool for datagrams we have no bespoke classes.  Better to make a bespoke class'''

    def __init__(self, fileptr, numberofbytes, typeofdatagram):
        self.typeofdatagram = typeofdatagram
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""

###############################################################################
    def read(self):
        self.data = self.fileptr.read(self.numberofbytes)

###############################################################################
class U_SVP:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'U'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = []

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHLLHH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.ProfileDate = s[8]
        self.Profiletime = s[9]
        self.NEntries = s[10]
        self.depthResolution = s[11]

        rec_fmt = '=LL'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        # i = 0
        for i in range(self.NEntries):
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            self.data.append(
                [float(s[0]) / float(100/self.depthResolution), float(s[1] / 10)])

        # read an empty byte
        self.fileptr.read(1)

        # now read the footer
        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)


###############################################################################
class X_depth:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'X'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""
        self.beams = []
###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLL4Hf2Hf4B'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = s[5]/1000
        self.counter = s[6]
        self.serialnumber = s[7]

        self.heading = float(s[8] / 100)
        self.soundspeedattransducer = float(s[9] / 10)
        self.transducerdepth = s[10]
        self.nbeams = s[11]
        self.nvaliddetections = s[12]
        self.samplefrequency = s[13]
        self.scanninginfo = s[14]
        self.spare1 = s[15]
        self.spare2 = s[16]
        self.spare3 = s[17]

        # read every beam in a single block and decode it with iter_unpack.  this replaces the
        # original per-beam file read + struct.unpack hot loop, which dominated the profile.
        beam_struct = struct.Struct('=fffHBBBbh')
        beamdata = self.fileptr.read(beam_struct.size * self.nbeams)

        # transpose the decoded beams into per-field columns with a single C-level zip,
        # then build each list in one comprehension (far cheaper than per-beam appends).
        columns = list(zip(*beam_struct.iter_unpack(beamdata))) or [()] * 9

        # NaN is the only value that is not equal to itself - cheaper than calling math.isnan
        self.depth = [0.0 if v != v else v for v in columns[0]]
        self.acrosstrackdistance = [0.0 if v != v else v for v in columns[1]]
        self.alongtrackdistance = [0.0 if v != v else v for v in columns[2]]
        self.detectionwindowslength = list(columns[3])
        self.qualityfactor = list(columns[4])
        self.beamincidenceangleadjustment = [v / 10.0 for v in columns[5]]
        self.detectioninformation = list(columns[6])
        self.realtimecleaninginformation = list(columns[7])
        self.reflectivity = [v / 10.0 for v in columns[8]]

        rec_unpack = struct.Struct('=BBH').unpack_from
        s = rec_unpack(self.fileptr.read(4))

        self.etx = s[1]
        self.checksum = s[2]

###############################################################################
    def encode(self):
        '''Encode a depth XYZ datagram record'''

        header_fmt = '=LBBHLL4Hf2Hf4B'
        header_len = struct.calcsize(header_fmt)

        fulldatagram = bytearray()

        rec_fmt = '=fffHBBBbh'
        rec_len = struct.calcsize(rec_fmt)

        footer_fmt = '=BBH'
        footer_len = struct.calcsize(footer_fmt)

        fulldatagrambytecount = header_len + (rec_len*self.nbeams) + footer_len

        # pack the header
        recordtime = int(dateToSecondsSinceMidnight(
            from_timestamp(self.time))*1000)
        header = struct.pack(header_fmt, fulldatagrambytecount-4, self.stx, ord(self.typeofdatagram), self.emmodel, self.recorddate, recordtime, self.counter, self.serialnumber, int(self.heading * 100),
                             int(self.soundspeedattransducer * 10), self.transducerdepth, self.nbeams, self.nvaliddetections, self.samplefrequency, self.scanninginfo, self.spare1, self.spare2, self.spare3)
        fulldatagram = fulldatagram + header

        # pack the beam summary info
        for i in range(self.nbeams):
            bodyrecord = struct.pack(rec_fmt, self.depth[i], self.acrosstrackdistance[i], self.alongtrackdistance[i], self.detectionwindowslength[i], self.qualityfactor[i], int(
                self.beamincidenceangleadjustment[i]*10), self.detectioninformation[i], self.realtimecleaninginformation[i], int(self.reflectivity[i]*10), )
            fulldatagram = fulldatagram + bodyrecord

        systemdescriptor = 1
        tmp = struct.pack('=B', systemdescriptor)
        fulldatagram = fulldatagram + tmp

        # now pack the footer
        etx = 3
        checksum = 0

        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer

        return fulldatagram

###############################################################################
class Y_SEABEDIMAGE:
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'Y'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""
        self.ARC = {}
        self.BeamPointingAngle = []
        self._beams = None
        self._beamcols = None

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        # read the whole datagram once and parse beam descriptors + samples from the buffer.
        raw = self.fileptr.read(self.numberofbytes)

        hdr = struct.Struct('=LBBHLLHHfHhhHHH')
        s = hdr.unpack_from(raw, 0)

        # self.numberofbytes= s[0]
        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5]/1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.samplefrequency = s[8]
        self.rangeToNormalIncidence = s[9]
        self.NormalIncidence = s[10]
        self.ObliqueBS = s[11]
        self.TxBeamWidth = s[12]
        self.TVGCrossOver = s[13]
        self.NumBeams = s[14]

        # decode the beam descriptors into columns once.  building the per-beam cbeam objects
        # (and slicing the samples per beam) is deferred to the lazy 'beams' property because the
        # common consumers (loadseabedimage / get_seabed_image) only need numSamples and samples.
        beamstruct = struct.Struct('=bBHH')
        pos = hdr.size
        cols = list(zip(*beamstruct.iter_unpack(raw[pos:pos + beamstruct.size * self.NumBeams]))) or [(), (), (), ()]
        self._beamcols = cols
        self._beams = None
        self.numSamples = sum(cols[2])
        pos += beamstruct.size * self.NumBeams

        # all backscatter samples follow as int16 (0.1 dB)
        self.samples = struct.unpack_from('=%dh' % self.numSamples, raw, pos) if self.numSamples else ()

        # footer layout at the end of the record: [spare byte][etx][checksum]
        self.etx, self.checksum = struct.unpack_from('=BH', raw, len(raw) - 3)

###############################################################################
    @property
    def beams(self):
        '''cbeam objects (each with its samples) built on demand from the decoded columns.'''
        if self._beams is None:
            cols = self._beamcols or [(), (), (), ()]
            samples = self.samples
            beams = []
            idx = 0
            for sd, di, nsamp, csn in zip(cols[0], cols[1], cols[2], cols[3]):
                b = cbeam((sd, di, nsamp, csn), 0)
                b.samples = samples[idx: idx + nsamp]
                idx += nsamp
                beams.append(b)
            self._beams = beams
        return self._beams

###############################################################################
    @beams.setter
    def beams(self, value):
        self._beams = value

###############################################################################
    def encode(self):
        '''Encode a seabed image datagram record'''

        header_fmt = '=LBBHLLHHfHhhHHH'
        header_len = struct.calcsize(header_fmt)

        fulldatagram = bytearray()

        rec_fmt = '=bBHH'
        rec_len = struct.calcsize(rec_fmt)

        sample_fmt = '=' + str(self.numSamples) + 'h'
        sample_len = struct.calcsize(sample_fmt)

        footer_fmt = '=BBH'
        footer_len = struct.calcsize(footer_fmt)

        fulldatagrambytecount = header_len + \
            (rec_len*self.NumBeams) + sample_len + footer_len

        # pack the header
        recordtime = int(dateToSecondsSinceMidnight(
            from_timestamp(self.time))*1000)
        header = struct.pack(header_fmt, fulldatagrambytecount-4, self.stx, ord(self.typeofdatagram), self.emmodel, self.recorddate, recordtime, self.counter,
                             self.serialnumber, self.samplefrequency, self.rangeToNormalIncidence, self.NormalIncidence, self.ObliqueBS, self.TxBeamWidth, self.TVGCrossOver, self.NumBeams)
        fulldatagram = fulldatagram + header

        # pack the beam summary info
        s = []
        for i, b in enumerate(self.beams):
            bodyrecord = struct.pack(
                rec_fmt, b.sortingDirection, b.detectionInfo, b.numberOfSamplesPerBeam, b.centreSampleNumber)
            fulldatagram = fulldatagram + bodyrecord
            # using the takeoffangle, we need to look up the correction from the ARC and apply it to the samples.
            a = round(self.BeamPointingAngle[i], 0)
            correction = self.ARC[a]
            for sample in b.samples:
                s.append(int(sample + correction))
        sampleRecord = struct.pack(sample_fmt, *s)
        fulldatagram = fulldatagram + sampleRecord

        systemdescriptor = 1
        tmp = struct.pack('=B', systemdescriptor)
        fulldatagram = fulldatagram + tmp

        # now pack the footer
        etx = 3
        checksum = 0
        footer = struct.pack('=BH', etx, checksum)
        fulldatagram = fulldatagram + footer

        return fulldatagram

###############################################################################
class G_SURFACESOUNDSPEED:
    '''Surface sound speed datagram (type 'G', 0x47).  Holds the sound speed measured at the
    transducer head sampled regularly throughout the record.  Ref: Simrad EM Datagrams Oct 2013, Table 40.'''
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = 'G'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.soundspeed = []

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5] / 1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.NEntries = s[8]

        # each entry is [time in seconds since record start, sound speed in m/s]
        self.soundspeed = []
        entry_fmt = '=HH'
        entry_len = struct.calcsize(entry_fmt)
        entry_unpack = struct.Struct(entry_fmt).unpack
        for i in range(self.NEntries):
            e = entry_unpack(self.fileptr.read(entry_len))
            self.soundspeed.append([float(e[0]), float(e[1]) / 10.0])  # dm/s -> m/s

        # always leave the file pointer at the end of this record
        self.fileptr.seek(self.offset + self.numberofbytes, 0)

###############################################################################
class PU_STATUS:
    '''Processing Unit status datagram (type '1', 0x31).  Sent about once per second; reports
    sensor input health and the last received sensor values.  Ref: Simrad EM Datagrams Oct 2013, Table 53.'''
    def __init__(self, fileptr, numberofbytes):
        self.typeofdatagram = '1'
        self.offset = fileptr.tell()
        self.numberofbytes = numberofbytes
        self.fileptr = fileptr
        self.fileptr.seek(numberofbytes, 1)
        self.data = ""

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHHHLLLLLLbbbbbBHhhhHLhBBbbbBHBBHhhhbBH'
        rec_len = struct.calcsize(rec_fmt)

        # guard against shorter (older firmware) variants so the scan stays aligned
        if self.numberofbytes < rec_len:
            self.fileptr.seek(self.offset + self.numberofbytes, 0)
            self.stx = 0
            self.emmodel = 0
            self.recorddate = self.recorddate if hasattr(self, 'recorddate') else 0
            return

        rec_unpack = struct.Struct(rec_fmt).unpack_from
        s = rec_unpack(self.fileptr.read(rec_len))

        self.stx = s[1]
        self.typeofdatagram = chr(s[2])
        self.emmodel = s[3]
        self.recorddate = s[4]
        self.time = float(s[5] / 1000.0)
        self.counter = s[6]
        self.serialnumber = s[7]
        self.pingrate = float(s[8] / 100.0)                 # centiHz -> Hz
        self.pingcounter = s[9]
        self.achievedswathdistance = s[10]                  # in 10% steps, 0-255
        self.sensorinputstatusUDP2 = s[11]
        self.sensorinputstatusserial1 = s[12]
        self.sensorinputstatusserial2 = s[13]
        self.sensorinputstatusserial3 = s[14]
        self.sensorinputstatusserial4 = s[15]
        self.ppsstatus = s[16]
        self.positionstatus = s[17]
        self.attitudestatus = s[18]
        self.clockstatus = s[19]
        self.headingstatus = s[20]
        self.pustatus = s[21]
        self.lastheading = float(s[22] / 100.0)             # 0.01 deg -> deg
        self.lastroll = float(s[23] / 100.0)
        self.lastpitch = float(s[24] / 100.0)
        self.lastheave = float(s[25] / 100.0)               # cm -> m
        self.soundspeedattransducer = float(s[26] / 10.0)   # dm/s -> m/s
        self.lastdepth = float(s[27] / 100.0)               # cm -> m
        self.alongshipvelocity = float(s[28] / 100.0)       # 0.01 m/s -> m/s
        self.attitudevelocitysensorstatus = s[29]
        self.mammalprotectionramp = s[30]
        self.backscatteratobliqueangle = s[31]              # dB
        self.backscatteratnormalincidence = s[32]           # dB
        self.fixedgain = s[33]                              # dB
        self.depthtonormalincidence = s[34]                 # m
        self.rangetonormalincidence = s[35]                 # m
        self.portcoverage = s[36]                           # deg
        self.stbdcoverage = s[37]                           # deg
        self.soundspeedfromprofile = float(s[38] / 10.0)    # dm/s -> m/s
        self.yawstabilisation = float(s[39] / 100.0)        # centideg -> deg
        self.portcoverageoracrossvelocity = s[40]
        self.stbdcoverageordownvelocity = s[41]
        self.cputemperature = s[42]                          # deg C (EM2040)
        self.etx = s[43]
        self.checksum = s[44]

        # always leave the file pointer at the end of this record
        self.fileptr.seek(self.offset + self.numberofbytes, 0)

###############################################################################
# time HELPER FUNCTIONS
###############################################################################

###############################################################################
def to_timestamp(dateObject):
    return (dateObject - datetime(1970, 1, 1)).total_seconds()

###############################################################################
def to_datetime(recorddate, recordtime):
    '''return a python date object from a split date and time record. works with kongsberg date and time structures'''
    date_object = datetime.strptime(
        str(recorddate), '%Y%m%d') + timedelta(0, recordtime)
    return date_object

###############################################################################
def from_timestamp(unixtime):
    return datetime.utcfromtimestamp(unixtime)

###############################################################################
def dateToKongsbergDate(dateObject):
    return dateObject.strftime('%Y%m%d')

###############################################################################
def dateToKongsbergtime(dateObject):
    return dateObject.strftime('%H%M%S')

###############################################################################
def dateToSecondsSinceMidnight(dateObject):
    return (dateObject - dateObject.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()

###############################################################################
# bitwise helper functions
###############################################################################

###############################################################################
def isBitSet(int_type, offset):
    '''testBit() returns a nonzero result, 2**offset, if the bit at 'offset' is one.'''
    mask = 1 << offset
    return (int_type & (1 << offset)) != 0


###############################################################################
def set_bit(value, bit):
    return value | (1 << bit)


###############################################################################
###############################################################################
if __name__ == "__main__":
    main()
