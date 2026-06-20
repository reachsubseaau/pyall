# name:              pyALL
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
import argparse
from datetime import datetime
from datetime import timedelta
import numpy as np

import geodetic
import logging
import timeseries
import ggmbes
import fileutils
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

    logging.basicConfig(filename=os.path.join(runtime_params['odir'], "all2point_log.txt"), level=logging.INFO)
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
            outfilename = depthtotif(filename, resolution=runtime_params['resolution'],
                                     value=runtime_params['value'], colour=runtime_params['colour'],
                                     epsg=requestedepsg, maxpings=int(runtime_params['debug']),
                                     verbose=runtime_params['verbose'], odir=runtime_params['odir'],
                                     colourmin=colourmin, colourmax=colourmax,
                                     keeprejected=runtime_params['keeprejected'])
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

    #we need to load the navigation to we can compute the position of the transducer at ping time...
    navigation = r.loadnavigation()
    nav = np.array(navigation)
    tslatitude = timeseries.cTimeSeries(nav[:,0], nav[:,1])
    tslongitude = timeseries.cTimeSeries(nav[:,0], nav[:,2])

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
            x, y, z, q, r_reflectivity = computebathypointcloud(datagram, geo)
            pointcloud.add(x, y, z, q, r_reflectivity)
            update_progress("Extracting Point Cloud", pingcounter/recordcount)
            pingcounter = pingcounter + 1

        if pingcounter == maxpings:
            break

    r.close()
    log("Load Duration: %.3f seconds" % (time.time() - start_time))

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
def computebathypointcloud(datagram, geo):
    '''using the MRZ datagram, efficiently compute a numpy array of the point clouds  '''

    datagram.east, datagram.north = geo.convertToGrid((datagram.longitude), datagram.latitude)
    # detection / realtime-cleaning flags let us know which soundings have been rejected
    detinfo = getattr(datagram, 'detectioninformation', None)
    rtclean = getattr(datagram, 'realtimecleaninginformation', None)
    for idx in range(datagram.nbeams):

        beam = ggmbes.GGBeam()
        beam.depth = datagram.depth[idx]
        beam.east, beam.north = geodetic.calculateGridPositionFromBearingDxDy(datagram.east, datagram.north, datagram.heading, datagram.acrosstrackdistance[idx], datagram.alongtrackdistance[idx])

        # beam.depth = (beam.z_reRefPoint_m + datagram.txTransducerDepth_m) * -1.0 #invert depths so we have negative depths.
        # beam.depth = beam.z_reRefPoint_m - datagram.z_waterLevelReRefPoint_m
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

    # report on RAW POINTS
    outfile = os.path.join(odir, os.path.basename(filename) + "_R.txt")
    np.savetxt(outfile, (xyz), fmt='%.10f', delimiter=',')

    # rasterise the point cloud into a floating point GeoTIFF
    outfilename = os.path.join(outfile + "_Raw_depth.tif")
    cloud2tif.saveastif(outfilename, geo, xyz, resolution=2, fill=False)

    log("Read complete at: %s" % (datetime.now()))
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
def computeapproximateresolution(filename, samplepings=10):
    '''estimate a sensible grid resolution (metres) from the across-track beam spacing of the first few depth pings.
    the raw spacing is snapped up to the next sensible interval (0.5, 1, 2, 5, 10, ...).'''
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
    return snapresolution(max(float(np.median(spacings)), 0.01))

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
def depthtotif(filename, resolution=0.0, value='depth', colour='none', epsg='0', outfilename='', fill=False, maxpings=-1, verbose=False, odir='', colourmin=None, colourmax=None, keeprejected=False):
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
    returns the path to the created GeoTIFF, or None if no data could be extracted.'''
    import rasterio
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

    runtime_params = {'epsg': epsg, 'debug': str(maxpings), 'verbose': bool(verbose), 'spherical': False, 'odir': ''}
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
        vmin = float(colourmin) if colourmin is not None else (float(valid.min()) if valid.size else 0.0)
        vmax = float(colourmax) if colourmax is not None else (float(valid.max()) if valid.size else 1.0)
        if vmax <= vmin:
            vmax = vmin + 1.0
        log("Colour palette range: %.3f to %.3f" % (vmin, vmax))
        norm = np.clip((arr - vmin) / (vmax - vmin), 0.0, 1.0)
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
def getfileinfo(filename):
    '''read a .all file and return a summary dictionary of datagram counts, approximate position and a suitable EPSG code.'''
    r = allreader(filename)
    longitude, latitude = r.getapproximatepositon()

    counts = {}
    starttimestamp = 0
    endtimestamp = 0
    recorddate = 0
    recordtime = 0
    first = True
    r.rewind()
    while r.moredata():
        numberofbytes, stx, typeofdatagram, emmodel, recorddate, recordtime = r.readdatagramheader()
        r.fileptr.seek(numberofbytes, 1)
        if first:
            starttimestamp = to_timestamp(to_datetime(recorddate, recordtime))
            first = False
        counts[typeofdatagram] = counts.get(typeofdatagram, 0) + 1
    if not first:
        endtimestamp = to_timestamp(to_datetime(recorddate, recordtime))
    r.rewind()
    r.close()

    epsg = geodetic.epsgfromlonglat(longitude, latitude)
    info = {
        'filename': filename,
        'filesize': os.path.getsize(filename),
        'approxlongitude': longitude,
        'approxlatitude': latitude,
        'epsg': str(epsg),
        'starttimestamp': starttimestamp,
        'endtimestamp': endtimestamp,
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
            out.append({
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
            })
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
        self.numberentries = s[8]

        rec_fmt = '=HHhhhH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        # we need to store all the attitude data in a list
        self.Attitude = [0 for i in range(self.numberentries)]

        i = 0
        while i < self.numberentries:
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            # time,status,roll,pitch,heave,heading
            self.Attitude[i] = [self.recorddate, self.time +
                                float(s[0]/1000.0), s[1], s[2]/100.0, s[3]/100.0, s[4]/100.0, s[5]/100.0]
            i = i + 1

        rec_fmt = '=BBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

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

        self.depth = [0 for i in range(self.nbeams)]
        self.acrosstrackdistance = [0 for i in range(self.nbeams)]
        self.alongtrackdistance = [0 for i in range(self.nbeams)]
        self.beamdepressionangle = [0 for i in range(self.nbeams)]
        self.beamazmuthangle = [0 for i in range(self.nbeams)]
        self.range = [0 for i in range(self.nbeams)]
        self.qualityfactor = [0 for i in range(self.nbeams)]
        self.lengthofdetectionwindow = [0 for i in range(self.nbeams)]
        self.reflectivity = [0 for i in range(self.nbeams)]
        self.beamnumber = [0 for i in range(self.nbeams)]

        # now read the variable part of the Record
        if self.emmodel < 700:
            rec_fmt = '=H3h2H2BbB'
        else:
            rec_fmt = '=4h2H2BbB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        i = 0
        while i < self.nbeams:
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            self.depth[i] = float(s[0] / float(100))
            self.acrosstrackdistance[i] = float(s[1] / float(100))
            self.alongtrackdistance[i] = float(s[2] / float(100))
            self.beamdepressionangle[i] = float(s[3] / float(100))
            self.beamazmuthangle[i] = float(s[4] / float(100))
            self.range[i] = float(s[5] / float(100))
            self.qualityfactor[i] = s[6]
            self.lengthofdetectionwindow[i] = s[7]
            self.reflectivity[i] = float(s[8] / float(100))
            self.beamnumber[i] = s[9]

            # now do some sanity checks.  We have examples where the depth and Across track values are NaN
            if (math.isnan(self.depth[i])):
                self.depth[i] = 0
            if (math.isnan(self.acrosstrackdistance[i])):
                self.acrosstrackdistance[i] = 0
            if (math.isnan(self.alongtrackdistance[i])):
                self.alongtrackdistance[i] = 0
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

        self.BeamPointingAngle = [0 for i in range(self.NumReceiveBeams)]
        self.TransmitSectorNumber = [0 for i in range(self.NumReceiveBeams)]
        self.DetectionInfo = [0 for i in range(self.NumReceiveBeams)]
        self.DetectionWindow = [0 for i in range(self.NumReceiveBeams)]
        self.qualityfactor = [0 for i in range(self.NumReceiveBeams)]
        self.DCorr = [0 for i in range(self.NumReceiveBeams)]
        self.TwoWayTraveltime = [0 for i in range(self.NumReceiveBeams)]
        self.reflectivity = [0 for i in range(self.NumReceiveBeams)]
        self.realtimecleaninginformation = [
            0 for i in range(self.NumReceiveBeams)]
        self.Spare = [0 for i in range(self.NumReceiveBeams)]
        self.beamnumber = [0 for i in range(self.NumReceiveBeams)]

        # # now read the variable part of the Transmit Record
        rec_fmt = '=hHLLLHBB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        for i in range(self.NumTransmitSector):
            data = self.fileptr.read(rec_len)
            bytesRead += rec_len
            s = rec_unpack(data)
            self.TiltAngle[i] = float(s[0]) / 100.0
            self.Focusrange[i] = s[1] / 10
            self.SignalLength[i] = s[2]
            self.SectorTransmitDelay[i] = s[3]
            self.centrefrequency[i] = s[4]
            self.SignalBandwidth[i] = s[5]
            self.SignalWaveformID[i] = s[6]
            self.TransmitSectorNumberTX[i] = s[7]

        # now read the variable part of the recieve record
        rx_rec_fmt = '=hHBbBBhH'
        rx_rec_len = struct.calcsize(rx_rec_fmt)
        rx_rec_unpack = struct.Struct(rx_rec_fmt).unpack

        for i in range(self.NumReceiveBeams):
            data = self.fileptr.read(rx_rec_len)
            rx_s = rx_rec_unpack(data)
            bytesRead += rx_rec_len
            self.BeamPointingAngle[i] = float(rx_s[0]) / 100.0
            self.TwoWayTraveltime[i] = float(
                rx_s[1]) / (4 * self.samplefrequency)
            self.TransmitSectorNumber[i] = rx_s[2]
            self.reflectivity[i] = rx_s[3] / 2.0
            self.qualityfactor[i] = rx_s[4]
            self.DetectionWindow[i] = rx_s[5]
            self.beamnumber[i] = rx_s[6]

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
        rec_fmt = '=LBBHLLHHHbB'
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
        self.numberentries = s[8]
        self.Systemdescriptor = s[9]

        rec_fmt = '=HhhhHB'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        # we need to store all the attitude data in a list
        self.Attitude = [0 for i in range(self.numberentries)]

        i = 0
        while i < self.numberentries:
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            inputTelegramSize = s[5]
            data = self.fileptr.read(inputTelegramSize)
            self.Attitude[i] = [self.recorddate, self.time + s[0]/1000,
                                s[1], s[2]/100.0, s[3]/100.0, s[4]/100.0, s[5]/100.0, data]
            i = i + 1

        # # now spare byte only if necessary
        # if self.numberofbytes % 2 != 0:
        # self.fileptr.read(1)

        # read an empty byte
        self.fileptr.read(1)

        # now read the footer
        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)

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

        self.BeamPointingAngle = [0 for i in range(self.NumReceiveBeams)]
        self.TransmitSectorNumber = [0 for i in range(self.NumReceiveBeams)]
        self.DetectionInfo = [0 for i in range(self.NumReceiveBeams)]
        self.DetectionWindow = [0 for i in range(self.NumReceiveBeams)]
        self.qualityfactor = [0 for i in range(self.NumReceiveBeams)]
        self.DCorr = [0 for i in range(self.NumReceiveBeams)]
        self.TwoWayTraveltime = [0 for i in range(self.NumReceiveBeams)]
        self.reflectivity = [0 for i in range(self.NumReceiveBeams)]
        self.realtimecleaninginformation = [
            0 for i in range(self.NumReceiveBeams)]
        self.Spare = [0 for i in range(self.NumReceiveBeams)]

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

        # now read the variable part of the recieve record
        rx_rec_fmt = '=hBBHBbfhbB'
        rx_rec_len = struct.calcsize(rx_rec_fmt)
        rx_rec_unpack = struct.Struct(rx_rec_fmt).unpack

        for i in range(self.NumReceiveBeams):
            data = self.fileptr.read(rx_rec_len)
            rx_s = rx_rec_unpack(data)
            bytesRead += rx_rec_len
            self.BeamPointingAngle[i] = float(rx_s[0]) / float(100)
            self.TransmitSectorNumber[i] = rx_s[1]
            self.DetectionInfo[i] = rx_s[2]
            self.DetectionWindow[i] = rx_s[3]
            self.qualityfactor[i] = rx_s[4]
            self.DCorr[i] = rx_s[5]
            self.TwoWayTraveltime[i] = float(rx_s[6])
            self.reflectivity[i] = rx_s[7]
            self.realtimecleaninginformation[i] = rx_s[8]
            self.Spare[i] = rx_s[9]

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

        self.depth = [0 for i in range(self.nbeams)]
        self.acrosstrackdistance = [0 for i in range(self.nbeams)]
        self.alongtrackdistance = [0 for i in range(self.nbeams)]
        self.detectionwindowslength = [0 for i in range(self.nbeams)]
        self.qualityfactor = [0 for i in range(self.nbeams)]
        self.beamincidenceangleadjustment = [0 for i in range(self.nbeams)]
        self.detectioninformation = [0 for i in range(self.nbeams)]
        self.realtimecleaninginformation = [0 for i in range(self.nbeams)]
        self.reflectivity = [0 for i in range(self.nbeams)]

        # # now read the variable part of the Record
        rec_fmt = '=fffHBBBbh'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        for i in range(self.nbeams):
            data = self.fileptr.read(rec_len)
            s = rec_unpack(data)
            self.depth[i] = s[0]
            self.acrosstrackdistance[i] = s[1]
            self.alongtrackdistance[i] = s[2]
            self.detectionwindowslength[i] = s[3]
            self.qualityfactor[i] = s[4]
            self.beamincidenceangleadjustment[i] = float(s[5] / 10)
            self.detectioninformation[i] = s[6]
            self.realtimecleaninginformation[i] = s[7]
            self.reflectivity[i] = float(s[8] / 10)

            # now do some sanity checks.  We have examples where the depth and Across track values are NaN
            if (math.isnan(self.depth[i])):
                self.depth[i] = 0
            if (math.isnan(self.acrosstrackdistance[i])):
                self.acrosstrackdistance[i] = 0
            if (math.isnan(self.alongtrackdistance[i])):
                self.alongtrackdistance[i] = 0

        rec_fmt = '=BBH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack_from
        data = self.fileptr.read(rec_len)
        s = rec_unpack(data)

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

###############################################################################
    def read(self):
        self.fileptr.seek(self.offset, 0)
        rec_fmt = '=LBBHLLHHfHhhHHH'
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
        self.samplefrequency = s[8]
        self.rangeToNormalIncidence = s[9]
        self.NormalIncidence = s[10]
        self.ObliqueBS = s[11]
        self.TxBeamWidth = s[12]
        self.TVGCrossOver = s[13]
        self.NumBeams = s[14]
        self.beams = []
        self.numSamples = 0
        self.samples = []

        rec_fmt = '=bBHH'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack

        self.numSamples = 0
        for i in range(self.NumBeams):
            s = rec_unpack(self.fileptr.read(rec_len))
            b = cbeam(s, 0)
            self.numSamples = self.numSamples + b.numberOfSamplesPerBeam
            self.beams.append(b)

        rec_fmt = '=' + str(self.numSamples) + 'h'
        rec_len = struct.calcsize(rec_fmt)
        rec_unpack = struct.Struct(rec_fmt).unpack
        self.samples = rec_unpack(self.fileptr.read(rec_len))

        # allocate the samples to the correct beams so it is easier to use
        sampleIDX = 0
        for b in self.beams:
            b.samples = self.samples[sampleIDX: sampleIDX +
                                     b.numberOfSamplesPerBeam]
            sampleIDX = sampleIDX + b.numberOfSamplesPerBeam

        # read an empty byte
        self.fileptr.read(1)

        # now read the footer
        self.etx, self.checksum = readfooter(self.numberofbytes, self.fileptr)

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
