import os
import sys
import math
import rasterio
from rasterio.transform import from_origin
from rasterio.transform import Affine
import numpy as np

import geodetic
import logging
import gc
from itertools import product

import rasterio
from rasterio.crs import CRS
from rasterio import windows

from scipy.signal import medfilt
from scipy.signal import medfilt2d

import fileutils

###############################################################################
def getsize(filename):
	with rasterio.open(filename) as src:
		pixels = src.height * src.width
		SRCRESOLUTION = src.res[0]
	gc.collect()	
	return pixels, SRCRESOLUTION 

###############################################################################
def get_tiles(ds, width=256, height=256):
    nols, nrows = ds.meta['width'], ds.meta['height']
    offsets = product(range(0, nols, width), range(0, nrows, height))
    big_window = windows.Window(col_off=0, row_off=0, width=nols, height=nrows)
    for col_off, row_off in  offsets:
        window =windows.Window(col_off=col_off, row_off=row_off, width=width, height=height).intersection(big_window)
        transform = windows.transform(window, ds.transform)
        yield window, transform

###############################################################################
def get_tiles2(ds, tile_width, tile_height, overlap):
	ncols, nrows = ds.meta['width'], ds.meta['height']
	xstep = tile_width - overlap
	ystep = tile_height - overlap
	for x in range(0, ncols, xstep):
		if x + tile_width > ncols:
			x = ncols - tile_width
		for y in range(0, nrows, ystep):
			if y + tile_height > nrows:
				y = nrows - tile_height
			window = windows.Window(x, y, tile_width, tile_height)
			transform = windows.transform(window, ds.transform)
			yield window, transform

###############################################################################
def tileraster(filename, odir, tilewidth = 512, tileheight = 512, tileoverlap= 10):
	'''use rasterio to tile a file into smaller manageable chunks'''

	outfilename = os.path.basename(filename) + "_TILE_"
	# odir = os.path.join(os.path.dirname(filename), os.path.splitext(os.path.basename(filename))[0] + "_TILED")
	makedirs(odir)

	with rasterio.open(filename) as src:
		metadata = src.meta.copy()
		log("Source file size is %d wide * %d high == %d pixels.  This is potentially too large for your system memory so we will tile it.." % (metadata['width'], metadata['height'], metadata['width'] * metadata['height']))
		idx = 0
		tilecount = len(list(get_tiles(src, tilewidth, tileheight)))
		log("Tiling into %s tiles..." % (tilecount))
		for window, transform in get_tiles(src, tilewidth, tileheight):
			metadata['transform'] = transform
			metadata['width'], metadata['height'] = window.width, window.height
			out_filepath = os.path.join(odir, outfilename + str(window.col_off) + "_" + str(window.row_off) + ".tif")
			idx += 1
			update_progress("Tiling to conserve memory...", idx / tilecount)
			# print(out_filepath)
			with rasterio.open(out_filepath, 'w', **metadata) as dst:
				dst.write(src.read(window=window))
	return odir
###############################################################################
def getWKT(filename):
		
	if not os.path.exists(filename):
		return
	
	with rasterio.open(filename) as src:
		WKT = src._crs.wkt
		# pkpk = CRS.from_epsg(4326).wkt
	src.close()
	#garbage collect
	gc.collect()	
	return WKT

# function to caluclate hillshade
###############################################################################
def hillshade(array,azimuth,angle_altitude):
	azimuth = 360.0 - azimuth 
	
	x, y = np.gradient(array)
	slope = np.pi/2. - np.arctan(np.sqrt(x*x + y*y))
	aspect = np.arctan2(-x, y)
	azm_rad = azimuth*np.pi/180. #azimuth in radians
	alt_rad = angle_altitude*np.pi/180. #altitude in radians

	shaded = np.sin(alt_rad)*np.sin(slope) + np.cos(alt_rad)*np.cos(slope)*np.cos((azm_rad - np.pi/2.) - aspect)
	
	return 255*(shaded + 1)/2

###############################################################################
def smoothtif(filename, outfilename, near=5):
	''' smooth a tif file using scipy. the near parameter is the size of the median filter.'''
	with rasterio.open(filename) as src:
		array = src.read(1)
		profile = src.profile

	# apply a 5x5 median filter to each band
	# filtered = medfilt(array, (1, 5, 5))
	filtered = medfilt2d(array, near)

	# Write to tif, using the same profile as the source
	with rasterio.open(outfilename, 'w', **profile) as dst:
		dst.write_band(1, filtered)

	#garbage collect
	gc.collect()	

	return outfilename

###############################################################################
def saveastif(outfilename, geo, pcd, resolution=1, fill=False):
	'''given a numpy array of point cloud, make a floating point geotif file using rasterio'''
	'''the numpy point clouds define the bounding box'''

	if len(pcd)==0:	
		return
		
	NODATA = -999
	xmin = pcd.min(axis=0)[0]
	ymin = pcd.min(axis=0)[1]
	zmin = pcd.min(axis=0)[2]
	
	xmax = pcd.max(axis=0)[0]
	ymax = pcd.max(axis=0)[1]
	zmax = pcd.max(axis=0)[2]

	xres 	= resolution
	yres 	= resolution
	width 	= math.ceil((xmax - xmin) / resolution)
	height 	= math.ceil((ymax - ymin) / resolution)

	transform = Affine.translation(xmin - xres / 2, ymin - yres / 2) * Affine.scale(xres, yres)
	
	log("Creating tif file... %s" % (outfilename))
	transform = from_origin(xmin, ymax, xres, yres)

	# save to file...
	src= rasterio.open(
			outfilename,
			mode="w",
			driver="GTiff",
			height=height,
			width=width,
			count=1,
			dtype='float32',
			crs=geo.projection.srs,
			transform=transform,
			nodata=NODATA,
	) 
	# populate the numpy array with the values....
	arr = np.full((height, width), fill_value=NODATA, dtype=float)
	
	from numpy import ma
	arr = ma.masked_values(arr, NODATA)

	for row in pcd:
		px = math.floor((row[0] - xmin) / xres)
		py = math.floor(height - (row[1] - ymin) / yres) - 1 #lord knows why -1
		# py, px = src.index(row[0], row[1])
		arr[py, px] = row[2]
		
	#we might want to fill in the gaps. useful sometimes...
	if fill:
		from rasterio.fill import fillnodata
		arr = fillnodata(arr, mask=None, max_search_distance=xres*2, smoothing_iterations=0)

	src.write(arr, 1)
	src.close()
	log("Creating tif file Complete.")

	return outfilename

###############################################################################
def pcd2meantif(outfilename, geo, pcd, resolution=1, fill=False):
    # Current (inefficient) code to quantize into XY 'bins' and take mean Z values in each bin

	if len(pcd)==0:	
		return
		
	pcd[:, 0:2] = np.round(pcd[:, 0:2]/float(resolution))*float(resolution) # Round XY values to nearest resolution value

	NODATA = -999
	xmin = pcd.min(axis=0)[0]
	ymin = pcd.min(axis=0)[1]
	
	xmax = pcd.max(axis=0)[0]
	ymax = pcd.max(axis=0)[1]

	xres 	= resolution
	yres 	= resolution
	width 	= math.ceil((xmax - xmin) / resolution)
	height 	= math.ceil((ymax - ymin) / resolution)
	mean_height = np.zeros((height, width))

	# Loop over each x-y bin and calculate mean z value 
	x_val = xmin
	for x in range(width):
		y_val = ymax
		for y in range(height):
			height_vals = pcd[(pcd[:,0] == float(x_val)) & (pcd[:,1] == float(y_val)), 2]
			if height_vals.size != 0:
				mean_height[y,x] = np.mean(height_vals)
			y_val -= resolution
		x_val += resolution

	# return mean_height
	arr = mean_height
	arr[mean_height == 0] = NODATA
	
	log("Creating tif file... %s" % (outfilename))
	transform = from_origin(xmin-(xres/2), ymax + (yres/2), xres, yres)

	# save to file...
	src= rasterio.open(
			outfilename,
			mode="w",
			driver="GTiff",
			height=height,
			width=width,
			count=1,
			dtype='float32',
			crs=geo.projection.srs,
			transform=transform,
			nodata=NODATA,
	) 
	#we might want to fill in the gaps. useful sometimes...
	if fill:
		from rasterio.fill import fillnodata
		arr = fillnodata(arr, mask=None, max_search_distance=xres*2, smoothing_iterations=0)

	src.write(arr, 1)
	src.close()
	log("Creating tif file Complete.")

	return outfilename

###############################################################################
def point2raster(outfilename, geo, pcd, resolution=1, bintype="mean", fill=False):
	'''given a numpy array of point cloud, make a floating point geotif file using rasterio'''
	'''the numpy point clouds define the bounding box'''
	# https://stackoverflow.com/questions/54842690/how-to-efficiently-convert-large-numpy-array-of-point-cloud-data-to-downsampled

	NODATA = -999
	
	if len(pcd)==0:
		return

	#take the point cloud array and transpose the xyz,xyz,xyz into xxx,yyy so we can bin them efficienctly without looping thru the data
	xy = pcd.T[:2]
	#bin the xy data into buckets.  at present this is only integer based so 1m resolution is minimum
	xy = ((xy + resolution / 2) // resolution).astype(int)
	# xy = ((xy - resolution / 2) // resolution).astype(int)
	#compute the range of the data 
	mn, mx = xy.min(axis=1), xy.max(axis=1)
	#compute the size of the data
	sz = mx + 1 - mn

	if bintype == 'mean':
		#Converts a tuple of index arrays into an array of flat indices, applying boundary modes to the multi-index.
		#RETURNS An array of indices into the flattened version of an array of dimensions dims.
		flatidx = np.ravel_multi_index(xy-mn[:, None], dims=sz)
		#compute the mean of each bin as efficiently as possible
		histo = np.bincount(flatidx, pcd[:, 2], sz.prod()) / np.maximum(1, np.bincount(flatidx, None, sz.prod()))
		arr = histo.reshape(sz).T
		arr = np.flip(arr, axis = 0)

	if bintype == 'count':
		#Converts a tuple of index arrays into an array of flat indices, applying boundary modes to the multi-index.
		#RETURNS An array of indices into the flattened version of an array of dimensions dims.
		flatidx = np.ravel_multi_index(xy-mn[:, None], dims=sz)
		#we can compute the count rapidly as well...
		histo = np.maximum(0, np.bincount(flatidx, None, sz.prod()))
		arr = histo.reshape(sz).T
		arr = np.flip(arr, axis = 0)

	if bintype == 'median':
		#calculate the medians...
		#https://stackoverflow.com/questions/10305964/quantile-median-2d-binning-in-python
		# Median is a bit harder
		flatidx = np.ravel_multi_index(xy-mn[:, None], dims=sz)
		order = flatidx.argsort()
		bin = flatidx[order]
		w = pcd[:, 2][order]
		edges = (bin[1:] != bin[:-1]).nonzero()[0] + 1
		# Median 
		median = [np.median(i) for i in np.split(w, edges)]
		#construct BINSxBINS matrix with median values
		binvals=np.unique(bin)
		medvals=np.zeros([sz.prod()])
		medvals[binvals]=median
		medvals=medvals.reshape(sz)
		arr = np.asarray(medvals).reshape(sz).T
		arr = np.flip(arr, axis = 0)

	if bintype == 'stddev':
		#https://stackoverflow.com/questions/10305964/quantile-median-2d-binning-in-python
		# Median is a bit harder
		flatidx = np.ravel_multi_index(xy-mn[:, None], dims=sz)
		order = flatidx.argsort()
		bin = flatidx[order]
		w = pcd[:, 2][order]
		edges = (bin[1:] != bin[:-1]).nonzero()[0] + 1
		# Standard Deviation
		stddev = [np.std(i) for i in np.split(w, edges)]
		#construct BINSxBINS matrix with median values
		binvals=np.unique(bin)
		sdvals=np.zeros([sz.prod()])
		sdvals[binvals]=stddev
		sdvals=sdvals.reshape(sz)
		arr = np.asarray(sdvals).reshape(sz).T
		arr = np.flip(arr, axis = 0)

	# clear out the empty nodes and set to NODATA value
	arr[arr == 0] = NODATA

	xmin = mn[0]
	ymin = mn[1]
	xmax = mx[0]
	ymax = mx[1]
	xres 	= resolution
	yres 	= resolution
	
	width 	= math.ceil((xmax - xmin) / resolution)
	height 	= math.ceil((ymax - ymin) / resolution)

	log("Creating tif file... %s" % (outfilename))
	transform = from_origin(xmin-(xres/2), ymax + (yres/2), xres, yres)

	# save to file...
	src= rasterio.open(
			outfilename,
			mode="w",
			driver="GTiff",
			height=height,
			width=width,
			count=1,
			dtype='float32',
			crs=geo.projection.srs,
			transform=transform,
			nodata=NODATA,
	) 
	#we might want to fill in the gaps. useful sometimes...
	if fill:
		from rasterio.fill import fillnodata
		arr = fillnodata(arr, mask=None, max_search_distance=xres*2, smoothing_iterations=0)

	src.write(arr, 1)
	src.close()
	log("Creating tif file Complete.")

	return outfilename
###############################################################################
def	log(msg, error = False, printmsg=True):
		if error == False:
			logging.info(msg)
		else:
			logging.error(msg)

###############################################################################
# def	createprj(outfilename, epsg, wkt=""):
def	createprj(outfilename, wkt=""):
	'''create the PRJ file'''

	# geo = geodetic.geodesy(epsg)
	# prj = open(outfilename, "w")
	# prj.writelines(geo.projection.crs.to_wkt(version="WKT1_ESRI", pretty=True))
	# prj.close()

	prj = open(outfilename, "w")
	prj.writelines(wkt)
	prj.close()

###############################################################################
###############################################################################
def	makedirs(odir):
	if not os.path.isdir(odir):
		os.makedirs(odir, exist_ok=True)
###############################################################################
def update_progress(job_title, progress):
	'''progress value should be a value between 0 and 1'''
	length = 20 # modify this to change the length
	block = int(round(length*progress))
	msg = "\r{0}: [{1}] {2}%".format(job_title, "#"*block + "-"*(length-block), round(progress*100, 2))
	if progress >= 1: msg += " DONE\r\n"
	sys.stdout.write(msg)
	sys.stdout.flush()
