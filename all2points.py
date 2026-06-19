#name:		  	all2points
#created:		October 2023
#by:			paul.kennedy@guardiangeomatics.com
#description:   python module to read a Kongsberg ALL file, create a point cloud

#done##########################################

#todo##########################################

import os.path
from argparse import ArgumentParser
from datetime import datetime, timedelta
import math
import numpy as np
# import open3d as o3d
import sys
import time
import rasterio
import multiprocessing as mp
import shapefile
import logging

import pyall
import fileutils
import geodetic
import multiprocesshelper 
import cloud2tif
import lashelper
import ggmbesstandard

###########################################################################
def as_runtime_params(parsed_args):
	'''normalize parsed runtime options into a serializable dictionary.'''
	if isinstance(parsed_args, dict):
		return parsed_args.copy()
	if hasattr(parsed_args, "__dict__"):
		return vars(parsed_args).copy()
	return {}

###########################################################################
def main():

	iho = ggmbesstandard.sp44()
	msg = str(iho.getordernames())

	parser = ArgumentParser(description='Read a ALL file and create point clouds.')
	parser.add_argument('-epsg', 	action='store', 		default="0",	dest='epsg', 			help='Specify an output EPSG code for transforming from WGS84 to East,North,e.g. -epsg 4326')
	parser.add_argument('-i', 		action='store',			default="", 	dest='inputfolder', 		help='Input filename/folder to process.')
	parser.add_argument('-cpu', 	action='store', 		default='0', 	dest='cpu', 			help='number of cpu processes to use in parallel. [Default: 0, all cpu]')
	parser.add_argument('-odir', 	action='store', 		default="",	dest='odir', 			help='Specify a relative output folder e.g. -odir GIS')
	parser.add_argument('-debug', 	action='store', 		default="500",	dest='debug', 			help='Specify the number of pings to process.  good only for debugging. [Default:-1]')
	parser.add_argument('-tvu', 	action='store_true', 		default=False,	dest='tvu', 			help='Use the Total Vertical Uncertainty cleaning algorithm')
	parser.add_argument('-verbose', 	action='store_true', 	default=False,		dest='verbose',			help='verbose to write LAZ files and other supproting file.s  takes some additional time!,e.g. -verbose [Default:false]')
	parser.add_argument('-standard',action='store', 		default="order1a",	dest='standard',		help='(optional) Specify the IHO SP44 survey order so we can set the filters to match the required specification. Select from :' + ''.join(msg) + ' [Default:order1a]' )
	parser.add_argument('-near', 	action='store', 		default="7",		dest='near',			help='(optional) ADVANCED:Specify the MEDIAN filter kernel width for computation of the regional surface so nearest neighbours can be calculated. [Default:5]')

	matches = []
	args = parser.parse_args()
	runtime_params = as_runtime_params(args)
	runtime_params.setdefault('spherical', False)

	if os.path.isfile(runtime_params['inputfolder']):
		matches.append(runtime_params['inputfolder'])

	if len(runtime_params['inputfolder']) == 0:
		# no file is specified, so look for a .pos file in the current folder.
		inputfolder = os.getcwd()
		matches = fileutils.findFiles2(False, inputfolder, "*.all")

	if os.path.isdir(runtime_params['inputfolder']):
		matches = fileutils.findFiles2(False, runtime_params['inputfolder'], "*.all")

	if len(matches) == 0:
		log("No input .all files found, skipping.", error=True)
		return

	#make sure we have a folder to write to
	runtime_params['inputfolder'] = os.path.dirname(matches[0])

	#make an output folder
	if len(runtime_params['odir']) == 0:
		runtime_params['odir'] = os.path.join(runtime_params['inputfolder'], str("all2point_%s" % (time.strftime("%Y%m%d-%H%M%S"))))
	makedirs(runtime_params['odir'])

	logging.basicConfig(filename=os.path.join(runtime_params['odir'], "all2point_log.txt"), level=logging.INFO)
	log("configuration: %s" % (str(runtime_params)))
	log("Output Folder: %s" % (runtime_params['odir']))

	results = []
	if str(runtime_params['cpu']) == '1':
		for file in matches:
			all2point(file, runtime_params)
	else:
		multiprocesshelper.log("Files to Import: %d" %(len(matches)))		
		cpu = multiprocesshelper.getcpucount(runtime_params['cpu'])
		log("Processing with %d CPU's" % (cpu))

		pool = mp.Pool(cpu)
		multiprocesshelper.g_procprogress.setmaximum(len(matches))
		poolresults = [pool.apply_async(all2point, (file, runtime_params), callback=multiprocesshelper.mpresult) for file in matches]
		pool.close()
		pool.join()
		# for idx, result in enumerate (poolresults):
		# 	results.append([file, result._value])
		# 	print (result._value)

############################################################
def all2point(filename, runtime_params):
	'''we will try to auto clean beams by extracting the beam xyzF flag data and attempt to clean in scipy'''
	'''we then set the beam flags to reject files we think are outliers and write the all file to a new file'''

	#load the python proj projection object library if the user has requested it
	if runtime_params['epsg'] != "0":
		geo = geodetic.geodesy(runtime_params['epsg'])
	else:
		runtime_params['epsg'] = pyall.getsuitableepsg(filename)
		geo = geodetic.geodesy(runtime_params['epsg'])

	log("Processing file: %s" % (filename))

	maxpings = int(runtime_params['debug'])
	if maxpings == -1:
		maxpings = 999999999

	pingcounter = 0
	beamcountarray = 0
	
	log("Loading Point Cloud...")
	pointcloud = pyall.loaddata(filename, runtime_params)
	xyz = np.column_stack([pointcloud.xarr, pointcloud.yarr, pointcloud.zarr, pointcloud.qarr, pointcloud.idarr])

	#report on RAW POINTS
	outfile = os.path.join(runtime_params['odir'], os.path.basename(filename) + "_R.txt")
	np.savetxt(outfile, (xyz), fmt='%.10f', delimiter=',')

	outfilename = os.path.join(outfile + "_Raw_depth.tif")
	cloud2tif.saveastif(outfilename, geo, xyz, resolution=2, fill=False)

	log("Read complete at: %s" % (datetime.now()))
	return outfilename

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
def	makedirs(odir):
	if not os.path.isdir(odir):
		os.makedirs(odir, exist_ok=True)

###############################################################################
def	log(msg, error = False, printmsg=True):
		if printmsg:
			print (msg)
		if error == False:
			logging.info(msg)
		else:
			logging.error(msg)

###############################################################################
if __name__ == "__main__":
		main()
