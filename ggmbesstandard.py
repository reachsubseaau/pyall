#name:			ggmbesstandard
#created:		July 2017
#by:			p.kennedy@guardiangeomatics.com
#description:	python module to represent MBES data STANDARDS
 
import math
import pprint
import rasterio
import numpy as np
import logging
import gc

###############################################################################
class sp44:
	'''used to hold the metadata associated with an IHO MBES standard.'''
	def __init__(self):
		self.name			= ""
		self.longitude 			= 0
		self.standards = []

		self.standards.append(standard("order2", 1.0, 0.023))
		self.standards.append(standard("order1b", 0.5, 0.013))
		self.standards.append(standard("order1a", 0.5, 0.013))
		self.standards.append(standard("specialorder", 0.25, 0.0075))
		self.standards.append(standard("exclusiveorder", 0.15, 0.0075))

		self.standards.append(standard("hipp1", 0.25, 0.0075))
		self.standards.append(standard("hipp2", 0.5, 0.013))
		self.standards.append(standard("hippassage", 1.0, 0.023))

	###############################################################################
	def __str__(self):
		return pprint.pformat(vars(self))

###############################################################################
	def getordernames(self):
		msg = []
		for rec in self.standards:
			msg.append(rec.name)
		return msg
	
###############################################################################
	def loadstandard(self, namerequired):
		for rec in self.standards:
			if namerequired in rec.name:
				return rec

###############################################################################
class standard:
	'''used to hold the metadata associated with an IHO MBES standard.'''
	def __init__(self, name, depthtvu_a, depthtvu_b ):
		self.name					= name
		self.depthtvu_a 			= depthtvu_a
		self.depthtvu_b 			= depthtvu_b

	###############################################################################
	def gettvuat(self, depth):
		'''TVU(d) = sqrt((a*a) + ( b * d)^2)'''
		tvud = math.sqrt((self.depthtvu_a * self.depthtvu_a) + (self.depthtvu_b * depth)**2)
		return tvud
	###############################################################################
	def details(self):
		msg = "Name:" + self.name + ",a=" + str(self.depthtvu_a) + ",b=" + str(self.depthtvu_b) + ",TVU(d)=sqrt((a*a)+(b*d)^2)"
		return msg

	###############################################################################
	def computeTVUSurface(self, filename, outfilename):
		'''compute the TVU for a surface array'''
		with rasterio.open(filename) as src:
			array = src.read(1)
			profile = src.profile
			NODATA = src.nodatavals[0]

		#now compute the TVU for the entire surface using numpy array mathmatics so its fast
		#preserve the NODATA value
		array[array==NODATA] = -9999
		arrayTVU = np.multiply (array, self.depthtvu_b)
		arrayTVU = np.square (arrayTVU, arrayTVU)
		arrayTVU = np.add (arrayTVU, (self.depthtvu_a*self.depthtvu_a))
		arrayTVU = np.sqrt(arrayTVU)

		#reset the nodata value...
		tmp = math.floor(self.gettvuat(-9999))
		arrayTVU[arrayTVU > tmp] = NODATA

		# Write to tif, using the same profile as the source
		with rasterio.open(outfilename, 'w', **profile) as dst:
			dst.write_band(1, arrayTVU)

		return outfilename

	###############################################################################
	def computeTVUBarometer(self, allowabletvufilename, uncertaintyfilename, outfilename):
		'''compute the TVU barometric pressure. A low pressure represents where the TVU for a survey point is well within specificaiton.  As high pressure is where the TVU is almost using all the allowable TVU'''
		with rasterio.open(allowabletvufilename) as allowedsrc:
			allowedarray = allowedsrc.read(1)
			allowedprofile = allowedsrc.profile
			allowedNODATA = allowedsrc.nodatavals[0]
			allowedarray[allowedarray==allowedNODATA] = -9999
		allowedsrc.close()
		#garbage collect
		gc.collect()	

		with rasterio.open(uncertaintyfilename) as uncertaintysrc:
			uncertaintyarray = uncertaintysrc.read(1)
			uncertaintyprofile = uncertaintysrc.profile
			uncertaintyNODATA = uncertaintysrc.nodatavals[0]
			uncertaintyarray[uncertaintyarray==uncertaintyNODATA] = 0
		uncertaintysrc.close()
		#garbage collect
		gc.collect()	
	
		#now compute the TVU barometric pressure for the entire surface using numpy array mathmatics so its fast
		# the TVUBAROMETER is the percentage of the allowable uncertainty compared to the actual uncertainty as computed by CUBE (or other software)
		# eg if the allowable uncertainty is 0.5m and the actual uncertainty is 0.25m then the TVUBAROMETER is 50%
		# eg if the allowable uncertainty is 0.5m and the actual uncertainty is 0.75m then the TVUBAROMETER is 150%
		# eg if the allowable uncertainty is 0.5m and the actual uncertainty is 0.5m then the TVUBAROMETER is 100%
		tvubarometerarray = np.divide (uncertaintyarray, allowedarray)
		tvubarometerarray = np.multiply (tvubarometerarray, 100)

		# Write to tif, using the same profile as the source
		with rasterio.open(outfilename, 'w', **allowedprofile) as dst:
			dst.write_band(1, tvubarometerarray)

		return outfilename

	###############################################################################
	def computeDeltaZ(self, regionalfilename, depthfilename, outfilename):
		'''compute the DeltaZ at all points in the surface.  Depta is difference between the point depth and the regional depth'''
		with rasterio.open(regionalfilename) as regionalsrc:
			regionalarray = regionalsrc.read(1)
			regionalprofile = regionalsrc.profile
			regionalNODATA = regionalsrc.nodatavals[0]
			regionalarray[regionalarray==regionalNODATA] = -9999
		regionalsrc.close()

		#garbage collect
		gc.collect()	

		with rasterio.open(depthfilename) as depthsrc:
			deptharray = depthsrc.read(1)
			depthprofile = depthsrc.profile
			depthNODATA = depthsrc.nodatavals[0]
			deptharray[deptharray==depthNODATA] = 9999
		depthsrc.close()

		#garbage collect
		gc.collect()	

		#now compute the TVU barometric pressure for the entire surface using numpy array mathmatics so its fast
		# deltaz = abs(griddepth-depth)
		deltazarray = np.subtract (regionalarray, deptharray)
		deltazarray = np.abs(deltazarray)

		deltazarray[deltazarray < -1000] = regionalNODATA
		deltazarray[deltazarray > 1000] = regionalNODATA
		# deltazarray[deltazarray == 0] = regionalNODATA

		# Write to tif, using the same profile as the source
		with rasterio.open(outfilename, 'w', **regionalprofile) as dst:
			dst.write_band(1, deltazarray)

		return outfilename

	###############################################################################
	def findoutliers(self, tvufilename, deltazfilename, outfilename):
		'''given a deltaz and tvu layer find the outliers by thresholding using the TVU array'''
		with rasterio.open(deltazfilename) as deltazsrc:
			deltazarray = deltazsrc.read(1)
			deltazprofile = deltazsrc.profile
			deltazNODATA = deltazsrc.nodatavals[0]
			height = deltazarray.shape[0]
			width = deltazarray.shape[1]
			cols, rows = np.meshgrid(np.arange(width), np.arange(height))
			xs, ys = rasterio.transform.xy(deltazsrc.transform, rows, cols)
			xs = np.float32(xs)
			ys = np.float32(ys)
			x = np.array(xs).flatten()
			y = np.array(ys).flatten()
			# deltazarray[deltazarray==deltazNODATA] = -9999
			del xs
			del ys
		deltazsrc.close()

		#garbage collect
		gc.collect()	

		with rasterio.open(tvufilename) as tvusrc:
			tvuarray = tvusrc.read(1)
			tvuprofile = tvusrc.profile
			tvuNODATA = tvusrc.nodatavals[0]
			# tvuarray[tvuarray== tvuNODATA] = 0
		tvusrc.close()

		#garbage collect
		gc.collect()	

		# make outlier array of difference in deltaz and tvu.  NEGATIVE values are not outliers.  only POSITVE VALUEs are outliers
		# log("Computing outliers...")
		outliersarray = np.subtract(deltazarray, tvuarray)
		outliersarray[outliersarray==deltazNODATA] = deltazNODATA
		outliersarray[outliersarray < 0] = 0

		valid = (outliersarray>0) & (deltazarray < 1000)
		deltaz = np.where(valid, deltazarray, 0)

		#clean up
		del deltazarray
		del tvuarray
		#garbage collect
		gc.collect()	

		dz = deltaz.flatten()
		xydz = np.stack((x,y,dz), axis=1, dtype=np.float32)
		#remove the values which are inliers
		xydz = xydz[np.all(xydz > 0.0, axis=1)]

		# Write to tif, using the same profile as the source
		# log("Writing outliers to raster file: %s" % (outfilename))
		with rasterio.open(outfilename, 'w', **deltazprofile) as dst:
			dst.write_band(1, outliersarray)

		return outfilename, xydz

	###############################################################################
	def	log(self, msg, error = False, printmsg=True):
			if error == False:
				logging.info(msg)
			else:
				logging.error(msg)

###############################################################################
def	log(msg, error = False, printmsg=True):
		if error == False:
			logging.info(msg)
		else:
			logging.error(msg)
