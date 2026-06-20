#name:		  	lashelper.py
#created:		jan 2020
#by:			paul.kennedy@guardiangeomatics.com
#description:   python module to spawn processes in relation to lastools
#copyright		Guardian Geomatics Pty Ltd
#				This software is explicitly prohibited by use of any non-guardian employee or subcontractor.
# 
##################
# #DONE
##################

import os
import shlex
import subprocess
import uuid
import sys
import time
import logging
import ctypes
import multiprocessing
import tempfile

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'shared'))
import geodetic
import fileutils

###############################################################################
def runner(cmd, verbose=False):
	'''process runner method.  pass the command to run and True if you want to real time verbose output of errors'''

	cmdname = cmd.split(" ")

	# log('Processing command %s' % (cmdname))

	args = shlex.split(cmd)

	stdout = []
	stderr = []
	popen = subprocess.Popen(args, stdout=subprocess.PIPE, universal_newlines=True, stderr=subprocess.PIPE)
	for stderr_line in iter(popen.stderr.readline, ""):
		stderr.append(stderr_line)
		if verbose:
			logging.info(stderr_line.rstrip())
	for stdout_line in iter(popen.stdout.readline, ""):
		stdout.append(stdout_line)
	popen.stdout.close()
	popen.stderr.close()

	popen.wait()

	return [stdout, stderr]

###############################################################################
def lassort(filename, odir=""):
	'''sort a laz file on gpstime'''

	if len(odir)==0:
		odir = os.path.dirname(filename)

	odirlog = makedirs(odir)

	root, ext = os.path.splitext(os.path.expanduser(os.path.basename(filename)))
	outfilename = os.path.join(odir, root  + "_S" + ext)
	outfilename = outfilename.replace('\\','/')

	cmd = "lassort.exe" + \
		" -i %s" % (filename) + \
		" -gps_time " + \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasmergelof2(listoffiles, filename, odir, rect=None):
	'''merge all files in a folder'''
	# odirlog = makedirs(odir)	

	outfilename = os.path.join(odir, filename + ".laz")
	outfilename = outfilename.replace('\\','/')

	if rect is None:
		clipper = ""
	else:
		#   -keep_xy 630000 4834000 631000 4836000 (min_x min_y max_x max_y)
		clipper = " -keep_xy %s %s %s %s" % (rect.left, rect.bottom, rect.right, rect.top)

	cmd = "lasmerge64.exe" + \
		" -lof %s" % (listoffiles) + \
		clipper + \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasmergelof(listoffiles, filename, odir):
	'''merge all files in a folder'''
	# odirlog = makedirs(odir)	

	outfilename = os.path.join(odir, filename + ".laz")
	outfilename = outfilename.replace('\\','/')

	cmd = "lasmerge64.exe" + \
		" -drop_x_below %s" % (str(10))+ \
		" -lof %s" % (listoffiles) + \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasmerge2(filespec, outfilename):
	'''merge all files in a folder'''

	cmd = "lasmerge64.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename


###############################################################################
def lasmerge(filespec, filename, odir):
	'''merge all files in a folder'''
	odirlog = makedirs(odir)	

	outfilename = os.path.join(odir, filename)
	outfilename = outfilename.replace('\\','/')

	cmd = "lasmerge64.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def las2asc(filename):
	'''export to XYZ records'''

	odirlog = makedirs(os.path.dirname(filename))	

	root = os.path.splitext(os.path.basename(filename))[0]

	outfilename = os.path.join(os.path.dirname(filename), root + ".txt")
	outfilename = outfilename.replace('\\','/')

	cmd = "las2txt64.exe" + \
		" -i %s" % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def txt2las(filename, epsg='4326'):
	'''import from XYZ record to a las file'''

	odirlog = makedirs(os.path.dirname(filename))	

	root = os.path.splitext(os.path.basename(filename))[0]

	filename = filename.replace('\\','/')
	outfilename = os.path.join(os.path.dirname(filename), root + ".laz")
	outfilename = outfilename.replace('\\','/')

	cmd = "txt2las.exe" + \
		" -i %s" % (filename) + \
		" -epsg %s" % (epsg) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	if epsg != '4326':
		outfilename = las2lasEPSG(outfilename, epsg=epsg)
	return outfilename

###############################################################################
def lasgrid(filename, resolution):
	'''use lasgrid to grid a file efficiently at the user specified resolution'''

	odirlog = makedirs(os.path.dirname(filename))	

	root = os.path.splitext(os.path.basename(filename))[0]

	outfilename = os.path.join(os.path.dirname(filename), root + ".tif")
	outfilename = outfilename.replace('\\','/')

	cmd = "lasgrid64.exe" + \
		" -i %s" % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -mem %s" % (str(1900)) + \
		" -step %s" % (str(resolution)) + \
		" -%s" % ('average') + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename
###############################################################################
def lasgrid4(filename, outfilename, resolution, epsg='31984'):
	'''use lasgrid to grid a file efficiently at the user specified resolution'''

	odirlog = makedirs(os.path.dirname(filename))	

	root = os.path.splitext(os.path.basename(filename))[0]

	if len(outfilename) == 0:
		outfilename = os.path.join(os.path.dirname(filename), root + ".tif")
		outfilename = outfilename.replace('\\','/')
	else:
		outfilename = outfilename.replace('\\','/')

	cmd = "lasgrid64.exe" + \
		" -i %s" % (filename) + \
		" -epsg %s" % (epsg) + \
		" -mem %s" % (str(1900)) + \
		" -step %s" % (str(resolution)) + \
		" -%s" % ('average') + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasgridsubcircle(filename, outfilename, resolution, epsg='31984', subcircle=1):
	'''use lasgrid to grid a file efficiently at the user specified resolution'''

	odirlog = makedirs(os.path.dirname(filename))	

	root = os.path.splitext(os.path.basename(filename))[0]

	if len(outfilename) == 0:
		outfilename = os.path.join(os.path.dirname(filename), root + ".tif")
		outfilename = outfilename.replace('\\','/')
	else:
		outfilename = outfilename.replace('\\','/')

	cmd = "lasgrid64.exe" + \
		" -i %s" % (filename) + \
		" -epsg %s" % (epsg) + \
		" -mem %s" % (str(1900)) + \
		" -step %s" % (str(resolution)) + \
		" -subcircle %s" % (str(subcircle)) + \
		" -%s" % ('average') + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def blast(srcfolder, dstfolder, filespec, resolution, outfilename, gridtype="hillshade", kill="3", outtype=".laz", RECT=None, epsg='31984'):
	'''make a slope raster file for QC purposes'''

	odirlog = makedirs(dstfolder)	

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')
	dstfolder = dstfolder.replace('\\','/')
	odirlog = makedirs(dstfolder)

	# if the user provides a bounding box, use it
	keepxy=""
	if RECT is not None:
		keepxy= " -keep_xy %.3f, %.3f, %.3f, %.3f" % (RECT.left, RECT.right, RECT.bottom, RECT.top)

	cmd = "blast2dem.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -odir %s" % (dstfolder)+ \
		" -step %s" % (str(resolution)) + \
		" -merged" + \
		" -float_precision 0.1" + \
		" -nbits 32" + \
		keepxy + \
		" -odir %s" % (dstfolder)+ \
		" -kill %s" % (str(kill)) + \
		" -epsg %s" % (epsg) + \
		" -%s" % (gridtype) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def blast2iso(srcfolder, filespec, dstfolder, outfilename, resolution, kill="3", RECT=None, epsg='31984'):
	'''make a contour map'''

	odirlog = makedirs(dstfolder)	

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')
	dstfolder = dstfolder.replace('\\','/')
	odirlog = makedirs(dstfolder)

	# if the user provides a bounding box, use it
	keepxy=""
	if RECT is not None:
		keepxy= " -keep_xy %.3f, %.3f, %.3f, %.3f" % (RECT.left, RECT.right, RECT.bottom, RECT.top)

	cmd = "blast2iso.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -odir %s" % (dstfolder)+ \
		" -iso_every %s" % (str(resolution)) + \
		" -smooth %s" % (str(float(resolution) * 10)) + \
		" -clean %s" % (str(resolution)) + \
		" -merged" + \
		keepxy + \
		" -odir %s" % (dstfolder)+ \
		" -kill %s" % (str(float(kill)* 10)) + \
		" -oshp" + \
		" -epsg %s" % (epsg) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	prjfilename = os.path.join(dstfolder, outfilename.replace('.shp','.prj')).replace('\\','/')
	geodetic.writePRJ(prjfilename, epsg)

	return outfilename

###############################################################################
def lasgrid2(srcfolder, dstfolder, filespec, resolution, outfilename, gridtype="average", fill="0", outtype=".laz", epsg='31984', verbose=False):
	'''grid a folder of laz files in a reliable manner'''

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')
	dstfolder = dstfolder.replace('\\','/')
	odirlog = makedirs(dstfolder)
	#outfilename = os.path.join(dstfolder, outtype)
	#outfilename = outfilename.replace('\\','/')
	#no point running multiple cores as we are emerging to a single file so MP is not appropriate.
	#tried splat
	#tried mem.  default for 64 bit is now 6GB
	cmd = "lasgrid64.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -step %s" % (str(resolution)) + \
		" -merged" + \
		" -odir %s" % (dstfolder)+ \
		" -%s" % (gridtype.lower()) + \
		" -fill %s" % (str(fill)) + \
		" -epsg %s" % (epsg) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, verbose)

		# " -average" + \
	# args = shlex.split(cmd)			
	# 	# " -fill %s" % (str(fill)) + \
	# 	# " -mem %s" % (2000)+ \

	return os.path.join(dstfolder, outfilename)

###############################################################################
def lasgrid3(srcfolder, dstfolder, filespec, resolution, outfilename, gridtype="average", fill="0", outtype=".laz", epsg='31984', verbose=False):
	'''grid a folder of laz files in a reliable manner'''

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')
	dstfolder = dstfolder.replace('\\','/')
	odirlog = makedirs(dstfolder)
	#outfilename = os.path.join(dstfolder, outtype)
	#outfilename = outfilename.replace('\\','/')
	#no point running multiple cores as we are emerging to a single file so MP is not appropriate.
	#tried splat
	#tried mem.  default for 64 bit is now 6GB
	cmd = "lasgrid64.exe" + \
		" -i %s" % (filespec) + \
		" -drop_x_below %s" % (str(10))+ \
		" -step %s" % (str(resolution)) + \
		" -merged" + \
		" -odir %s" % (dstfolder)+ \
		" -%s" % (gridtype.lower()) + \
		" -fill %s" % (str(fill)) + \
		" -epsg %s" % (epsg) + \
		" -v"+ \
		" -o %s" % (outfilename)

		# " -average" + \
	stdout, stderr = runner(cmd, verbose)
	
	return os.path.join(dstfolder, outfilename)

###############################################################################
def lasoverage(srcfolder, dstfolder, filespec, resolution, overageresolution, epsg='31984', verbose=False):
	'''clip out overlapping data from a series of files, to produce non-overlapped data files'''

	# make a list and sort instead of a wildcard.  maybe this helps how overage works?
	# files = findFiles2(False, srcfolder, "*.laz")
	# files.sort()
	# filespec = ""
	# for f in files:
	# 	filespec = filespec + " " + f

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	log("Processing with %d CPU's" %(cpu))

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')

	logging.info("******Overage files to process: %s" % (filespec))
	logging.info("******Overage output folder: %s" % (dstfolder))

	dstfolder = dstfolder.replace('\\','/')
	odirlog = makedirs(dstfolder)

	if float(overageresolution) == 0:
		cutresolution = float(resolution)
	else:
		cutresolution = float(overageresolution)
	logging.info("******Overage Resolution: %.3f, Grid Resolution %.3f" % (cutresolution, float(resolution)))

	# #we need to ensure we dont cause edge effects
	# resolution = float(resolution) / 4  #pkpk we needed to make this 1 for the cross lines in A14 as the infill did not work well.  not sure whats is happening yet.

	cmd = "lasoverage.exe" + \
		" -i %s" % (filespec) + \
		" -step %.3f" % (cutresolution) + \
		" -odir %s" % (dstfolder)+ \
		" -cpu64" + \
		strcores + \
		" -v" + \
		" -odix _overage" + \
		" -remove_overage" + \
		" -epsg %s" % (epsg) + \
		" -olaz"

	stdout, stderr = runner(cmd, verbose)

###############################################################################
def lasoveragenew(srcfolder, dstfolder, filespec, resolution=1, overageresolution=1, epsg='31984', verbose=False):

	'''clip out overlapping data from a series of files, to produce non-overlapped data files'''

	# make a list and sort instead of a wildcard.  maybe this helps how overage works?
	# files = findFiles2(False, srcfolder, "*.laz")
	# files.sort()
	# filespec = ""
	# for f in files:
	# 	filespec = filespec + " " + f

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	log("Processing with %d CPU's" %(cpu))

	filespec = os.path.join(srcfolder, filespec)
	filespec = filespec.replace('\\','/')

	logging.info("******Overage2 files to process: %s" % (filespec))
	logging.info("******Overage2 output folder: %s" % (dstfolder))

	dstfolder = dstfolder.replace('\\','/')
	# odirlog = makedirs(dstfolder)

	if float(overageresolution) == 0:
		cutresolution = float(resolution)
	else:
		cutresolution = float(overageresolution)
	logging.info("******Overage Resolution: %.3f, Grid Resolution %.3f" % (cutresolution, float(resolution)))

	# #we need to ensure we dont cause edge effects
	# resolution = float(resolution) / 4  #pkpk we needed to make this 1 for the cross lines in A14 as the infill did not work well.  not sure whats is happening yet.

	cmd = "lasoverage.exe" + \
		" -i %s" % (filespec) + \
		" -step %.3f" % (cutresolution) + \
		" -odir %s" % (dstfolder)+ \
		" -v" + \
		" -remove_overage" + \
		" -odix _overage" + \
		" -epsg %s" % (epsg) + \
		" -olaz" + \
		" -cpu64" + \
		strcores + \
		" -files_are_flightlines"
		
	stdout, stderr = runner(cmd, verbose)
	return [stdout, stderr]

###############################################################################
def lasduplicate2(filename, outfilename):
	'''remove duplicate records from a file and rename the file at the end'''

	# odirlog = makedirs(os.path.dirname(filename))	
	filename = filename.replace('\\','/')
	outfilename = outfilename.replace('\\','/')

	cmd = "lasduplicate64.exe" + \
		" -i %s" % (filename) + \
		" -olaz " + \
		" -o %s" % (outfilename)

		# " -drop_x_below %s" % (str(10))+ \
	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasduplicate(filename):
	'''remove duplicate records from a file and rename the file at the end'''

	odirlog = makedirs(os.path.dirname(filename))	

	outfilename = os.path.join(os.path.dirname(filename), "uniq.laz")
	outfilename = outfilename.replace('\\','/')

	cmd = "lasduplicate64.exe" + \
		" -i %s" % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	#now delete the original filename and rename the temp file to the original
	try:
		fileutils.deletefile(filename)
		os.rename(outfilename, filename)
	except:			
		log("Error while duplicating & renaming file %s" % (outfilename), True)

	return filename

###############################################################################
def hillshade(filename, odir, resolution):
	'''make a hillshade png file for QC purposes'''

	odirlog = makedirs(odir)	

	filename = os.path.abspath(filename).replace('\\','/')
	root = os.path.splitext(os.path.basename(filename))[0]
	outfilename = os.path.join(odir, root + '_hillshade.png')
	outfilename = outfilename.replace('\\','/')

	cmd = "blast2dem.exe" + \
		" -i %s" % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -step %s" % (str(resolution)) + \
		" -kill %s" % (str(resolution*10)) + \
		" -hillshade" + \
		" -opng " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasclipbb(filename, rect, odir, resolution, nodata=0, prefix=""):
	'''clip a laz file using a rectangle file'''
	#-keep_xy 630000 4834000 631000 4836000 (min_x min_y max_x max_y)

	odirlog = makedirs(odir)	

	filename = os.path.abspath(filename).replace('\\','/')
	root = os.path.splitext(os.path.basename(filename))[0]
	# outfilename = os.path.join(odir, prefix + '_clipped.laz')
	outfilename = os.path.join(odir, prefix + root + '_G_C.laz')
	outfilename = outfilename.replace('\\','/')

	#ensure the las file has positive up.  some files are positive down.  this is not so good so we can find out if the file has positive depthas and set blast to invert them on the fly
	scale_z = 1.0
	lasrect = getlazboundingbox(filename, odir)
	if ispositivedepths(lasrect):
		scale_z = -1.0
	
	cmd = "blast2dem.exe" + \
		" -i %s" % (filename) + \
		" -keep_xy %.3f %.3f %.3f %.3f" % (rect.left, rect.bottom, rect.right, rect.top) + \
		" -drop_x_below %s" % (str(10))+ \
		" -scale_z %s" % (str(scale_z)) + \
		" -step %s" % (str(resolution)) + \
		" -kill %s" % (str(resolution*100)) + \
		" -olaz " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasclip(filename, shp, odir, nodata=0, prefix="", rejectinterior=True):
	'''clip a laz file using a shape file'''

	odirlog = makedirs(odir)	

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 

	#decide if we are to keep the point inside or outside the area.
	if rejectinterior:
		rejectinterior = " -interior"
	else:
		rejectinterior = ""
	filename = os.path.abspath(filename).replace('\\','/')
	root = os.path.splitext(os.path.basename(filename))[0]
	outfilename = os.path.join(odir, prefix + '_clipped.laz')
	outfilename = os.path.join(odir, prefix + root + '_clipped.laz')
	outfilename = outfilename.replace('\\','/')
	shp = os.path.abspath(shp).replace('\\','/')
	cmd = "lasclip.exe" + \
		" -i %s" % (filename) + \
		rejectinterior + \
		" -cpu64 " + \
		" -donuts " + \
		strcores + \
		" -quiet " + \
		" -olaz " + \
		" -drop_x_below %s" % (str(10))+ \
		" -poly %s" % (shp) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasclip2(inputs, shp, odir, nodata=0, prefix="", rejectinterior=True):
	'''clip a laz file using a shape file'''

	# odirlog = makedirs(odir)	

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 

	odix = "_clipped"
	#decide if we are to keep the point inside or outside the area.
	if rejectinterior:
		rejectinterior = " -interior"
	else:
		rejectinterior = ""
	# filename = os.path.abspath(filename).replace('\\','/')
	# root = os.path.splitext(os.path.basename(filename))[0]
	# outfilename = os.path.join(odir, prefix + '_clipped.laz')
	# outfilename = os.path.join(odir, prefix + root + '_clipped.laz')
	# outfilename = outfilename.replace('\\','/')
	shp = os.path.abspath(shp).replace('\\','/')
	cmd = "lasclip.exe" + \
		" -i %s" % (inputs) + \
		rejectinterior + \
		" -cpu64 " + \
		" -donuts " + \
		strcores + \
		" -quiet " + \
		" -olaz " + \
		" -drop_x_below %s" % (str(10))+ \
		" -poly %s" % (shp) + \
		" -odir %s" % (odir) + \
		" -odix %s" % (odix)
		
		# " -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return
###############################################################################
def demzip2(filename, outfilename, nodata=0, replace=False):
	'''convert the 1-band tif file to a laz file '''

	odir = os.path.dirname(outfilename)
	odirlog = makedirs(odir)	

	#the file already exists and the user is not wanting to replace...
	if os.path.exists(outfilename) and replace == False:
		return outfilename

	if os.path.exists(outfilename) and replace == True:
		fileutils.deletefile(outfilename)

	cmd = "demzip.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -nodata_value %s" % (str(nodata)) + \
		" -olaz " + \
		" -o %s" % (outfilename.replace('\\','/'))

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def demzip(filename, odir, nodata=0, prefix="", replace=False):
	'''convert the 1-band tif file to a laz file '''

	odirlog = makedirs(odir)	

	root = os.path.splitext(os.path.basename(filename))[0]
	# outfilename = os.path.join(odir, prefix + '_1band.laz')
	outfilename = os.path.join(odir, prefix + root + '.laz')

	#the file already exists and the user is not wanting to replace...
	if os.path.exists(outfilename) and replace == False:
		return outfilename

	if os.path.exists(outfilename) and replace == True:
		fileutils.deletefile(outfilename)

	cmd = "demzip.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -nodata_value %s" % (str(nodata)) + \
		" -olaz " + \
		" -o %s" % (outfilename.replace('\\','/'))

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasindex(inputs, rebuild=False):
	'''index the laz files for performance'''

	inputs = os.path.abspath(inputs).replace('\\','/')+"/*.laz"

	# log('index the laz files for performance')
	odir = os.path.dirname(inputs)

	if rebuild == False:
		indexfiles = fileutils.findFiles2(False, odir, "*.lax")
		if len(indexfiles) > 0:
			return

	odirlog = makedirs(odir)	

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	# log("Indexing with %d CPU's" %(cpu))

	cmd = "lasindex.exe" + \
		" -i %s" % (inputs) + \
		strcores

	stdout, stderr = runner(cmd, False)

	return [stdout, stderr]

###############################################################################
def lastile(inputs, odir, tile_size=5000, prefix="", rebuild=False, verbose=False):
	'''tile the laz files to a regular grids so we can scale indefinitely'''

	inputs = os.path.abspath(inputs).replace('\\','/')+"/*.laz"

	log('Tile the laz files to a regular grids so we can scale indefinitely')

	#check to see if we really need to rebuild
	# if rebuild == True:
		# files = fileutils.findFiles2(False, odir, "*.laz")
	# 	ds.deletefolder(odir)
	# else:
	# 	if len(files) > 0:
	# 		#files exist so quit
	# 		return odir

	# starttime = time.time()
	# odirlog = makedirs(odir)	

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	strcores = " -cores 4" # pkpk
	log("Tiling %s with %d CPU's" %(inputs, cpu))

	cmd = "lastile.exe" + \
		" -i %s" % (inputs) + \
		" -cpu64 " + \
		strcores + \
		" -v" + \
		" -drop_x_below %s" % (str(10))+ \
		" -tile_size %s " % (str(tile_size)) + \
		" -olaz " + \
		" -odir %s" % (odir)

		# " -reversible " + \
	stdout, stderr = runner(cmd, verbose)

	# log("Tile Elapsed time: %.1f seconds" %(float(time.time() - starttime)), True)

	return odir

###############################################################################
def lasthin(filename, odir, resolution=1, fill=0, epsg='4326', prefix=""):
	'''convert the laz file to a coverage shp file'''

	odirlog = makedirs(odir)	

	root, ext = os.path.splitext(os.path.basename(filename))
	#outfilename = os.path.join(odir, prefix + '_1band.txt')
	outfilename = os.path.join(odir, prefix + root + '_thin'+ ext).replace('\\','/')

	resolution = float(resolution) * 3
	cmd = "lasthin64.exe" + \
		" -i %s" % (filename) + \
		" -step %s " % (str(resolution)) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasboundaries(inputs, odir, resolution=1, fill=0, epsg='4326', prefix="", verbose=False):
	'''convert the laz file to a coverage shp file'''

	odirlog = makedirs(odir)	

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	# log("Processing with %d CPU's" %(cpu))

	outfilename = os.path.join(odir, prefix + '_boundary.shp').replace('\\','/')

	# concavity mwith a smaller number more closely follows the tif file
	# disjoint forces individual polygons rather than connecting lines.

	concavity = float(resolution) + float(fill)
	concavity = float(fill)
	concavity = float(resolution) + float(fill) + float(fill)
	concavity = float(fill) + float(fill)
	concavity = max(concavity,1)

	cmd = "lasboundary.exe" + \
		" -i %s" % (inputs) + \
		" -drop_x_below %s" % (str(10))+ \
		" -concavity %s " % (str(concavity)) + \
		" -epsg %s " % (str(epsg)) + \
		" -cpu64 " + \
		strcores + \
		" -merged " + \
		" -labels " + \
		" -disjoint " + \
		" -holes" + \
		" -o %s" % (outfilename)

		# " -v" + \
	stdout, stderr = runner(cmd, verbose)

	return outfilename

###############################################################################
# def lasboundary(filename, odir, nodata=0, resolution=1, prefix="", outfilename = "", extension="txt"):
def lasboundary(filename, outfilename, nodata=0, resolution=1, replace=False):
	'''convert the laz file to a coverage shape file'''

	# odirlog = makedirs(os.path.dirname(outfilename))	

	# if len(outfilename) == 0:
	# 	root = os.path.splitext(os.path.basename(filename))[0]
	# 	#outfilename = os.path.join(odir, prefix + '_1band.txt')
	# 	outfilename = os.path.join(odir, prefix + root + '_boundary.'+ extension).replace('\\','/')

	#the file already exists and the user is not wanting to replace...
	if os.path.exists(outfilename) and replace == False:
		return outfilename

	cpu = getcpucount(0)
	strcores = " -cores %s" % (cpu) 
	log("Processing with %d CPU's" %(cpu))

	#lasboundary -i pk.laz -disjoint -o pkdisjoint.txt 
	# concavity mwith a smaller number more closely follows the tif file
	# disjoint forces individual polygons rather than connecting lines.

	nodatamin = float(nodata) - 0.1
	nodatamax = float(nodata) + 0.1
	resolution = float(resolution) * 1.5

	cmd = "lasboundary.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -drop_x_below %s" % (str(10))+ \
		" -concavity %s " % (str(resolution)) + \
		" -cpu64 " + \
		strcores + \
		" -v" + \
		" -disjoint " + \
		" -drop_z %s %s" % (str(nodatamin), str(nodatamax)) + \
		" -o %s" % (outfilename.replace('\\','/'))

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def lasoverlap(filename1, filename2, odir, resolution=1, prefix=""):
	'''compute the overlap between 2 files'''

	odirlog = makedirs(odir)	

	filename1 = os.path.abspath(filename1).replace('\\','/')
	filename2 = os.path.abspath(filename2).replace('\\','/')

	root1 = os.path.splitext(os.path.basename(filename1))[0]
	root2 = os.path.splitext(os.path.basename(filename2))[0]
	outfilename = os.path.join(odir, prefix + "_" + root1 + "_" + root2 + '.laz')
	outfilename = outfilename.replace('\\','/')

	cmd = "lasoverlap64.exe" + \
		" -i %s %s" % (filename1, filename2) + \
		" -drop_x_below %s" % (str(10))+ \
		" -values " + \
		" -faf " + \
		" -no_over " + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def las2lasEPSG(filename, odir="", epsg="4326"):
	'''apply EPSG code to a las file'''

	filename = os.path.abspath(filename).replace('\\','/')

	root = os.path.splitext(os.path.basename(filename))[0]
	outfilename = os.path.join(odir, root + "_EPSG_" + epsg + '.laz')
	outfilename = outfilename.replace('\\','/')

	outfilename = os.path.join(os.path.dirname(filename), root + ".laz")
	# outfilename = os.path.join(os.path.dirname(filename), root + "_EPSG_" + epsg + ".laz")
	outfilename = outfilename.replace('\\','/')

	prefix = str(uuid.uuid1())
	path = os.path.join(os.path.dirname(filename), prefix + ".laz")
	fileutils.copyfile(filename, path)

	cmd = "las2las64.exe" + \
		" -i %s " % (path) + \
		" -epsg %s " % (epsg) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)
	fileutils.deletefile(path)

	return outfilename


###############################################################################
def las2las(filename, odir, zcorrection=0, suffix="_Z"):
	'''correct a laz file by adding a user supplied correction'''

	# odirlog = makedirs(odir)	

	filename = os.path.abspath(filename).replace('\\','/')

	root = os.path.splitext(os.path.basename(filename))[0]
	outfilename = os.path.join(odir, root + suffix + '.laz')
	outfilename = outfilename.replace('\\','/')

	cmd = "las2las64.exe" + \
		" -i %s " % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -translate_z %.3f " % (zcorrection) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def las2lasclipstarttime(filename, outfilename, clipstarttime=0):
	'''correct a laz file by adding a user supplied correction'''

	# odirlog = makedirs(odir)

	filename = os.path.abspath(filename).replace('\\','/')
	outfilename = os.path.abspath(outfilename).replace('\\','/')

	cmd = "las2las64.exe" + \
		" -i %s " % (filename) + \
		" -drop_x_below %s" % (str(10))+ \
		" -drop_gpstime_below %.3f " % (clipstarttime) + \
		" -o %s" % (outfilename)

	stdout, stderr = runner(cmd, False)

	return outfilename

###############################################################################
def ispositivedepths(rect):
	if rect.minz > 0 or rect.maxz > 0:
		return True
	if rect.minz < 0 or rect.maxz < 0:
		return False
###############################################################################
def getlazfirstlast(filename, odir, prefix="las2txt"):
	'''get the laz first and last records as fast as possible'''
	'''las2txt -i CATHX_D2019-08-29T00-36-18-702Z_None_X0.laz -thin_with_grid 1 -stdout | more'''

	if not os.path.isdir(odir):
		os.makedirs(odir)

	outfilename = os.path.join(odir, prefix + '_info.txt')
	fstd = open(outfilename, 'w')

	cmd = "las2txt64.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -o %s" % (os.path.abspath(outfilename).replace('\\','/')) + \
		" -thin_with_grid 1"

	args = shlex.split(cmd)

	proc = subprocess.Popen(args, stdout=fstd, stderr=subprocess.PIPE)	
	proc.wait()

	'''now extract the position informaiton '''
	startx = 0
	starty = 0
	startz = 0
	endx = 0
	endy = 0
	endz = 0

	f = open(outfilename, 'r')
	for line in f:
		line = line.strip()
		words = line.split(" ")
		if startx == 0:
			startx = float(words[0])
			starty = float(words[1])
			startz = float(words[2])

		endx = float(words[0])
		endy = float(words[1])
		endz = float(words[2])

	return startx, starty, startz, endx, endy, endz

###############################################################################
def getlazmeandepth(filename, odir, prefix="lasinfo"):
	'''get the laz metadata mean depth'''
	'''lasinfo -i lidar.laz -o _info -otxt -histo z 1'''

	if not os.path.isdir(odir):
		os.makedirs(odir)

	prefix = str(uuid.uuid1())
	outfilename = os.path.join(odir, prefix + '_infomd.txt').replace('\\','/')
	fstd = open(outfilename, 'w')

	cmd = "lasinfo64.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -o %s" % (os.path.abspath(outfilename).replace('\\','/')) + \
		" -nv" + \
		" -histo z 1 " 
	args = shlex.split(cmd)

	proc = subprocess.Popen(args, stdout=fstd, stderr=subprocess.PIPE)	
	proc.wait()
	fstd.close()

	'''now extract the position information '''
	#point data format:          0
	meanz = 0
	recordcount = 0
	f = open(outfilename, 'r')
	for line in f:
		if line.lstrip().lower().startswith('number of point records'):
			line = line.lstrip()
			line = line.rstrip()
			line = line.replace("(", "")
			line = line.replace(")", "")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace(",", "")
			words = line.split(" ")
			recordcount= float(words[4])
			if recordcount == 0:
				return meanz, recordcount

		if line.lstrip().lower().startswith('average z'):
			line = line.lstrip()
			line = line.rstrip()
			line = line.replace("(", "")
			line = line.replace(")", "")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace(",", "")
			words = line.split(" ")
			meanz= float(words[3])
	f.close()
	fileutils.deletefile(outfilename)
	return meanz, recordcount

###############################################################################
def getlazboundingbox(filename, odir, prefix="lasinfo"):
	'''get the laz metadata and return the bounding box rectangle'''
	'''lasinfo -i lidar.laz -odix _info -otxt'''

	x = 0
	y = 0

	if not os.path.isdir(odir):
		os.makedirs(odir)

	prefix = str(uuid.uuid1())
	outfilename = os.path.join(odir, prefix + '_infobb.txt')
	f = open(outfilename, 'w')

	cmd = "lasinfo64.exe" + \
		" -i %s" % (os.path.abspath(filename).replace('\\','/')) + \
		" -o %s" % (os.path.abspath(outfilename).replace('\\','/')) + \
		" -nv" + \
		" -nc" 
	args = shlex.split(cmd)

	proc = subprocess.Popen(args, stdout=f, stderr=subprocess.PIPE)	
	proc.wait()

	'''now extract the position informaiton '''
	#   scale factor x y z:         0.0001 0.0001 0.0001
	#   offset x y z:               467000 7786000 1000
	#   min x y z:                  467106.6526 7786476.5504 1612.2718
	#   max x y z:                  467161.9851 7786494.3574 1612.8824


	p1 = POINT(0,0)
	p2 = POINT(1,1)
	rectangle = RECT(p1, p2)
	
	f = open(outfilename, 'r')
	for line in f:
		if line.lstrip().lower().startswith('min x'):
			line = line.lstrip()
			line = line.rstrip()
			line = line.replace("(", "")
			line = line.replace(")", "")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace(",", "")
			words = line.split(" ")
			x = float(words[4])
			y = float(words[5])
			z = float(words[6])
			ll = POINT(x,y,z)
		if line.lstrip().lower().startswith('max x'):
			line = line.lstrip()
			line = line.rstrip()
			line = line.replace("(", "")
			line = line.replace(")", "")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace("  ", " ")
			line = line.replace(",", "")
			words = line.split(" ")
			x = float(words[4])
			y = float(words[5])
			z = float(words[6])
			ur = POINT(x,y,z)
			rectangle = RECT(ll, ur)	

	f.close()		
	fileutils.deletefile(outfilename)
	return rectangle

###############################################################################
def	makedirs(odir):
	if not os.path.isdir(odir):
		os.makedirs(odir, exist_ok=True)
	odirlog = os.path.join(odir, "log").replace('\\','/')
	if not os.path.isdir(odirlog):
		os.makedirs(odirlog)
	return odirlog

###############################################################################
def getcpucount(requestedcpu):
	'''control how many CPU's we use for multi processing'''
	if int(requestedcpu) == 0:
		requestedcpu = multiprocessing.cpu_count()

		stat = MEMORYSTATUSEX()
		ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
		# print("MemoryLoad: %d%%" % (stat.dwMemoryLoad))
		# print("MemoryAvailable: %d%%" % (stat.ullAvailPhys/(1024*1024*1024)))
		availablememoryingigs = stat.ullAvailPhys/(1024*1024*1024)
		# make sure we have enough memory per CPU
		requiredgigspercpu = 4

		maxcpu = max(1, int(availablememoryingigs/ requiredgigspercpu))
		# ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
		# print("MemoryLoad: %d%%" % (stat.dwMemoryLoad))
		# ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
		# print("MemoryLoad: %d%%" % (stat.dwMemoryLoad))
		# ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
		# print("MemoryLoad: %d%%" % (stat.dwMemoryLoad))
		# ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
		# print("MemoryLoad: %d%%" % (stat.dwMemoryLoad))

		if int(requestedcpu) > maxcpu:
			requestedcpu = maxcpu
	return int(requestedcpu)

###############################################################################
def	log(msg, error = False, printmsg=True):
		if error == False:
			logging.info(msg)
		else:
			logging.error(msg)

###############################################################################
class POINT(object):
	def __init__(self, x, y, z=0.0):
		self.x = x
		self.y = y
		self.z = z

###############################################################################
class RECT(object):
	def __init__(self, p1, p2):
		'''Store the top, bottom, left and right values for points 
				p1 and p2 are the (corners) in either order
		'''
		self.left   = min(p1.x, p2.x)
		self.right  = max(p1.x, p2.x)
		self.bottom = min(p1.y, p2.y)
		self.top    = max(p1.y, p2.y)
		self.minz   = min(p1.z, p2.z)
		self.maxz   = max(p1.z, p2.z)

class MEMORYSTATUSEX(ctypes.Structure):
	_fields_ = [
		("dwLength", ctypes.c_ulong),
		("dwMemoryLoad", ctypes.c_ulong),
		("ullTotalPhys", ctypes.c_ulonglong),
		("ullAvailPhys", ctypes.c_ulonglong),
		("ullTotalPageFile", ctypes.c_ulonglong),
		("ullAvailPageFile", ctypes.c_ulonglong),
		("ullTotalVirtual", ctypes.c_ulonglong),
		("ullAvailVirtual", ctypes.c_ulonglong),
		("sullAvailExtendedVirtual", ctypes.c_ulonglong),
	]

	def __init__(self):
		# have to initialize this to the size of MEMORYSTATUSEX
		self.dwLength = ctypes.sizeof(self)
		super(MEMORYSTATUSEX, self).__init__()


###################################################################################################
if __name__ == "__main__":
	logging.info("lashelper.py copyright GuardianGeomatics Pty Ltd")
