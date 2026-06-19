#!/usr/bin/python
#
# ---------------------------------------------------------------------
# |																	 |
# |	geodetic.cc -  a collection of geodetic functions			   |
# |	Paul Kennedy May 2016										   |
# |	Jim Leven  - Dec 99											 |
# |																	 |
# | originally from:													|
# | http://wegener.mechanik.tu-darmstadt.de/GMT-Help/Archiv/att-8710/Geodetic_py |
# |ftp://pdsimage2.wr.usgs.gov/pub/pigpen/Python/Geodetic_py.py		|
# |																	 |
# ---------------------------------------------------------------------
#
# ------------------------------------------------------------------------------
# | Algrothims from Geocentric Datum of Australia Technical Manual			|
# | 												|
# | http://www.anzlic.org.au/icsm/gdatum/chapter4.html					|
# | 												|
# | This page last updated 11 May 1999 								|
# | 												|
# | Computations on the Ellipsoid									|
# | 												|
# | There are a number of formulae that are available		   		|
# | to calculate accurate geodetic positions, 							|
# | azimuths and distances on the ellipsoid.							|
# | 												|
# | Vincenty's formulae (Vincenty, 1975) may be used 						|
# | for lines ranging from a few cm to nearly 20,000 km, 					|
# | with millimetre accuracy. 									|
# | The formulae have been extensively tested 								|
# | for the Australian region, by comparison with results	   		|
# | from other formulae (Rainsford, 1955 & Sodano, 1965). 					|
# |												|
# | * Inverse problem: azimuth and distance from known 					|
# |			latitudes and longitudes 					|
# | * Direct problem: Latitude and longitude from known 					|
# |			position, azimuth and distance. 				|
# | * Sample data 										|
# | * Excel spreadsheet 											|
# | 												|
# | Vincenty's Inverse formulae										|
# | Given: latitude and longitude of two points				 		|
# |			(latitude1, longitude1 and latitude2, longitude2), 	|
# | Calculate: the ellipsoidal distance (s) and 						|
# | forward and reverse azimuths between the points (alpha1Tp2, alpha21).	|
# |											|
# ------------------------------------------------------------------------------

import math
import numpy as np
import sys
import os.path
import pyproj
import logging

###############################################################################
def main():

	easting = 10
	northing = 10
	distance = 5
	logging.info(calculateGridPositionFromrangeBearing(easting, northing, distance, 0))
	logging.info(calculateGridPositionFromrangeBearing(easting, northing, distance, 90))
	logging.info(calculateGridPositionFromrangeBearing(easting, northing, distance, 180))
	logging.info(calculateGridPositionFromrangeBearing(easting, northing, distance, 270))
	logging.info(calculateGridPositionFromrangeBearing(easting, northing, distance, 360))

	f = 1.0 / 298.257223563		# WGS84
	a = 6378137.0 			# metres

	logging.info("\n Ellipsoidal major axis =  %12.3f metres\n" % ( a ))
	logging.info("\n Inverse flattening	 =  %15.9f\n" % ( 1.0/f ))

	logging.info("\n Test Flinders Peak to Buninyon")
	logging.info("\n ****************************** \n")
	latitude1 = -(( 3.7203 / 60. + 57) / 60. + 37 )
	longitude1 = ( 29.5244 / 60. + 25) / 60. + 144
	logging.info("Flinders Peak coordinates loaded")
	deg = int(latitude1)
	min = int(abs( ( latitude1 - deg) * 60.0 ))
	sec = abs(latitude1 * 3600 - deg * 3600) - min * 60
	logging.info(" Flinders Peak =   %3i\xF8%3i\' %6.3f\",  " % ( deg, min, sec ))
	deg = int(longitude1)
	min = int(abs( ( longitude1 - deg) * 60.0 ))
	sec = abs(longitude1 * 3600 - deg * 3600) - min * 60
	logging.info(" %3i\xF8%3i\' %6.3f\" \n" % ( deg, min, sec ))

	latitude2 = -(( 10.1561 / 60. + 39) / 60. + 37 )
	longitude2 = ( 35.3839 / 60. + 55) / 60. + 143
	logging.info("\n Buninyon coordinates loaded\n")

	deg = int(latitude2)
	min = int(abs( ( latitude2 - deg) * 60.0 ))
	sec = abs(latitude2 * 3600 - deg * 3600) - min * 60
	logging.info(" Buninyon	  =   %3i\xF8%3i\' %6.3f\",  " % ( deg, min, sec ))
	deg = int(longitude2)
	min = int(abs( ( longitude2 - deg) * 60.0 ))
	sec = abs(longitude2 * 3600 - deg * 3600) - min * 60
	logging.info(" %3i\xF8%3i\' %6.3f\" \n" % ( deg, min, sec ))

	# dist, alpha1Tp2, alpha21   = vinc_dist  ( f, a, latitude1, longitude1, latitude2,  longitude2 )
	dist, alpha1Tp2, alpha21 = calculaterangeBearingFromGeographicals(longitude1, latitude1,  longitude2,  latitude2 )

	logging.info("\n Ellipsoidal Distance = %15.3f metres\n			should be		 54972.271 m\n" % ( dist ))
	logging.info("\n Forward and back azimuths = %15.6f, %15.6f \n" % ( alpha1Tp2, alpha21 ))
	deg = int(alpha1Tp2)
	min = int( abs(( alpha1Tp2 - deg) * 60.0 ) )
	sec = abs(alpha1Tp2 * 3600 - deg * 3600) - min * 60
	logging.info(" Forward azimuth = %3i\xF8%3i\' %6.3f\"\n" % ( deg, min, sec ))
	deg = int(alpha21)
	min = int(abs( ( alpha21 - deg) * 60.0 ))
	sec = abs(alpha21 * 3600 - deg * 3600) - min * 60
	logging.info(" Reverse azimuth = %3i\xF8%3i\' %6.3f\"\n" % ( deg, min, sec ))

	# Test the direct function */
	latitude1 = -(( 3.7203 / 60. + 57) / 60. + 37 )
	longitude1 = ( 29.5244 / 60. + 25) / 60. + 144
	dist = 54972.271
	alpha1Tp2 = ( 5.37 / 60. + 52) / 60. + 306
	latitude2 = longitude2 = 0.0
	alpha21 = 0.0

	# latitude2, longitude2, alpha21 = vincentyDirect (latitude1, longitude1, alpha1Tp2, dist )
	latitude2, longitude2, alpha21 = calculateGeographicalPositionFromrangeBearing(latitude1, longitude1, alpha1Tp2, dist)

	logging.info("\n Projected point coordinates computed\n")
	deg = int(latitude2)
	min = int(abs( ( latitude2 - deg) * 60.0 ))
	sec = abs( latitude2 * 3600 - deg * 3600) - min * 60
	logging.info(" Projected Point = %3i\xF8%3i\' %6.3f\", " % ( deg, min, sec ))
	deg = int(longitude2)
	min = int(abs( ( longitude2 - deg) * 60.0 ))
	sec = abs(longitude2 * 3600 - deg * 3600) - min * 60
	logging.info("  %3i\xF8%3i\' %6.3f\"\n" % ( deg, min, sec ))
	logging.info(" Should be Buninyon \n" )
	logging.info("\n Reverse azimuth computed\n")
	deg = int(alpha21)
	min = int(abs( ( alpha21 - deg) * 60.0 ))
	sec = abs(alpha21 * 3600 - deg * 3600) - min * 60
	logging.info(" Reverse azimuth = %3i\xF8%3i\' %6.3f\"\n\n" % ( deg, min, sec ))

###############################################################################
def epsgfromlonglat (longitude, latitude):
	from pyproj import CRS
	from pyproj.aoi import AreaOfInterest
	from pyproj.database import query_utm_crs_info

	utm_crs_list = query_utm_crs_info(
		datum_name="WGS 84",
		area_of_interest=AreaOfInterest(
			west_lon_degree=longitude,
			south_lat_degree=latitude,
			east_lon_degree=longitude,
			north_lat_degree=latitude,
		),
	)
	utm_crs = CRS.from_epsg(utm_crs_list[0].code)
	return utm_crs_list[0].code

###############################################################################
def medfilt (x, k):
	"""Apply a length-k median filter to a 1D array x.
	Boundaries are extended by repeating endpoints.
	"""
	assert k % 2 == 1, "Median filter length must be odd."
	assert x.ndim == 1, "Input must be one-dimensional."
	k2 = (k - 1) // 2
	y = np.zeros ((len (x), k), dtype=x.dtype)
	y[:,k2] = x
	for i in range (k2):
		j = k2 - i
		y[j:,i] = x[:-j]
		y[:j,i] = x[0]
		y[:-j,-(i+1)] = x[j:]
		y[-j:,-(i+1)] = x[-1]
	return np.median (y, axis=1)

###############################################################################
# from: http://mathforum.org/library/drmath/view/62034.html
def calculaterangeBearingFromGridPosition(easting1, northing1, easting2, northing2):
	"""given 2 east, north, pairs, compute the range and bearing"""

	dx = easting2-easting1
	dy = northing2-northing1

	bearing = 90 - (180/math.pi)*math.atan2(northing2-northing1, easting2-easting1)
	return (math.sqrt((dx*dx)+(dy*dy)), bearing)

###############################################################################
def normalize360(brg):
	brg = brg % 360
	if (brg < 0):
		brg += 360
	return brg

###############################################################################
# taken frm http://gis.stackexchange.com/questions/76077/how-to-create-points-based-on-the-distance-and-bearing-from-a-survey-point
def calculateGridPositionFromrangeBearing(easting, northing, distance, bearing):
	"""given an east, north, range and bearing, compute a new coordinate on the grid"""
	point =   (easting, northing)
	angle =   90 - bearing
	bearing = math.radians(bearing)
	angle =   math.radians(angle)

	# polar coordinates
	dist_x = distance * math.cos(angle)
	dist_y = distance * math.sin(angle)

	xfinal = point[0] + dist_x
	yfinal = point[1] + dist_y

	# direction cosines
	cosa = math.cos(angle)
	cosb = math.cos(bearing)
	xfinal = point[0] + (distance * cosa)
	yfinal = point[1] + (distance * cosb)

	return [xfinal, yfinal]

##############################################################################
def calculateGridPositionFromBearingDxDy(x, y, heading, dx, dy):
	'''given a grid position, heading, Dx(metres) and Dy(metres), compute a new grid position.  handle zero length x,y gracefully '''

	# compute along the heading using the dy coordinate
	if dy != 0:
		x2,y2 = calculateGridPositionFromrangeBearing(x,y, dy, heading)
	else:
		x2 = x
		y2 = y

	# compute along the heading using the dx coordinate
	if dx != 0:
		x,y = calculateGridPositionFromrangeBearing(x2,y2, dx, heading+90)
	else:
		x = x2
		y = y2
	return x, y

##############################################################################
def calculateGeographicalPositionFromBearingDxDy(longitude, latitude, heading, dx, dy):
	'''given a geographical position, heading, Dx(metres) and Dy(metres), compute a new geographical position.  handle zero length x,y gracefully '''

	# compute along the heading using the dy coordinate
	if dy != 0:
		lat, lon, az = calculateGeographicalPositionFromrangeBearing(latitude, longitude, heading, dy)
	else:
		lon = longitude
		lat = latitude

	# compute along the heading using the dx coordinate
	if dx != 0:
		lat, lon, az = calculateGeographicalPositionFromrangeBearing(lat, lon, heading + 90.0, dx)
	else:
		lon = longitude
		lat = latitude
	return lon, lat

###############################################################################
def calculaterangeBearingFromGeographicals(longitude1, latitude1,  longitude2,  latitude2 ) :
		"""
		Returns s, the distance between two geographic points on the ellipsoid
		and alpha1, alpha2, the forward and reverse azimuths between these points.
		lats, longs and azimuths are in decimal degrees, distance in metres

		Returns ( s, alpha1Tp2,  alpha21 ) as a tuple
		"""
		f = 1.0 / 298.257223563		# WGS84
		a = 6378137.0 			# metres

		if (abs( latitude2 - latitude1 ) < 1e-8) and ( abs( longitude2 - longitude1) < 1e-8 ) :
				return 0.0, 0.0, 0.0

		piD4   = math.atan( 1.0 )
		two_pi = piD4 * 8.0

		latitude1	= latitude1 * piD4 / 45.0
		longitude1 = longitude1 * piD4 / 45.0		# unfortunately lambda is a key word!
		latitude2	= latitude2 * piD4 / 45.0
		longitude2 = longitude2 * piD4 / 45.0

		b = a * (1.0 - f)

		TanU1 = (1-f) * math.tan( latitude1 )
		TanU2 = (1-f) * math.tan( latitude2 )

		U1 = math.atan(TanU1)
		U2 = math.atan(TanU2)

		lembda = longitude2 - longitude1
		last_lembda = -4000000.0		# an impossibe value
		omega = lembda

		# Iterate the following equations,
		#  until there is no significant change in lembda

		while ( last_lembda < -3000000.0 or lembda != 0 and abs( (last_lembda - lembda)/lembda) > 1.0e-9 ) :

				sqr_sin_sigma = pow( math.cos(U2) * math.sin(lembda), 2) + \
						pow( (math.cos(U1) * math.sin(U2) - \
						math.sin(U1) *  math.cos(U2) * math.cos(lembda) ), 2 )

				Sin_sigma = math.sqrt( sqr_sin_sigma )

				Cos_sigma = math.sin(U1) * math.sin(U2) + math.cos(U1) * math.cos(U2) * math.cos(lembda)

				sigma = math.atan2( Sin_sigma, Cos_sigma )

				Sin_alpha = math.cos(U1) * math.cos(U2) * math.sin(lembda) / math.sin(sigma)
				alpha = math.asin( Sin_alpha )

				Cos2sigma_m = math.cos(sigma) - (2 * math.sin(U1) * math.sin(U2) / pow(math.cos(alpha), 2) )

				C = (f/16) * pow(math.cos(alpha), 2) * (4 + f * (4 - 3 * pow(math.cos(alpha), 2)))

				last_lembda = lembda

				lembda = omega + (1-C) * f * math.sin(alpha) * (sigma + C * math.sin(sigma) * \
						(Cos2sigma_m + C * math.cos(sigma) * (-1 + 2 * pow(Cos2sigma_m, 2) )))

		u2 = pow(math.cos(alpha),2) * (a*a-b*b) / (b*b)

		A = 1 + (u2/16384) * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))

		B = (u2/1024) * (256 + u2 * (-128+ u2 * (74 - 47 * u2)))

		delta_sigma = B * Sin_sigma * (Cos2sigma_m + (B/4) * \
				(Cos_sigma * (-1 + 2 * pow(Cos2sigma_m, 2) ) - \
				(B/6) * Cos2sigma_m * (-3 + 4 * sqr_sin_sigma) * \
				(-3 + 4 * pow(Cos2sigma_m,2 ) )))

		s = b * A * (sigma - delta_sigma)

		alpha1Tp2 = math.atan2( (math.cos(U2) * math.sin(lembda)), \
				(math.cos(U1) * math.sin(U2) - math.sin(U1) * math.cos(U2) * math.cos(lembda)))

		alpha21 = math.atan2( (math.cos(U1) * math.sin(lembda)), \
				(-math.sin(U1) * math.cos(U2) + math.cos(U1) * math.sin(U2) * math.cos(lembda)))

		if ( alpha1Tp2 < 0.0 ) :
				alpha1Tp2 =  alpha1Tp2 + two_pi
		if ( alpha1Tp2 > two_pi ) :
				alpha1Tp2 = alpha1Tp2 - two_pi

		alpha21 = alpha21 + two_pi / 2.0
		if ( alpha21 < 0.0 ) :
				alpha21 = alpha21 + two_pi
		if ( alpha21 > two_pi ) :
				alpha21 = alpha21 - two_pi

		alpha1Tp2	= alpha1Tp2	* 45.0 / piD4
		alpha21	= alpha21	* 45.0 / piD4
		return s, alpha1Tp2,  alpha21

   # END of Vincenty's Inverse formulae


###############################################################################
#-------------------------------------------------------------------------------
# Vincenty's Direct formulae							|
# Given: latitude and longitude of a point (latitude1, longitude1) and 			|
# the geodetic azimuth (alpha1Tp2) 						|
# and ellipsoidal distance in metres (s) to a second point,			|
# 										|
# Calculate: the latitude and longitude of the second point (latitude2, longitude2) 	|
# and the reverse azimuth (alpha21).						|
# 										|
#-------------------------------------------------------------------------------
def calculateGeographicalPositionFromrangeBearing(latitude1, longitude1, alpha1To2, s) :
		"""
		Returns the lat and long of projected point and reverse azimuth
		given a reference point and a distance and azimuth to project.
		lats, longs and azimuths are passed in decimal degrees

		Returns ( latitude2,  longitude2,  alpha2To1 ) as a tuple

		"""
		f = 1.0 / 298.257223563		# WGS84
		a = 6378137.0 			# metres

		piD4 = math.atan( 1.0 )
		two_pi = piD4 * 8.0

		latitude1	= latitude1	* piD4 / 45.0
		longitude1 = longitude1 * piD4 / 45.0
		alpha1To2 = alpha1To2 * piD4 / 45.0
		if ( alpha1To2 < 0.0 ) :
				alpha1To2 = alpha1To2 + two_pi
		if ( alpha1To2 > two_pi ) :
				alpha1To2 = alpha1To2 - two_pi

		b = a * (1.0 - f)

		TanU1 = (1-f) * math.tan(latitude1)
		U1 = math.atan( TanU1 )
		sigma1 = math.atan2( TanU1, math.cos(alpha1To2) )
		Sinalpha = math.cos(U1) * math.sin(alpha1To2)
		cosalpha_sq = 1.0 - Sinalpha * Sinalpha

		u2 = cosalpha_sq * (a * a - b * b ) / (b * b)
		A = 1.0 + (u2 / 16384) * (4096 + u2 * (-768 + u2 * \
				(320 - 175 * u2) ) )
		B = (u2 / 1024) * (256 + u2 * (-128 + u2 * (74 - 47 * u2) ) )

		# Starting with the approximation
		sigma = (s / (b * A))

		last_sigma = 2.0 * sigma + 2.0	# something impossible

		# Iterate the following three equations
		#  until there is no significant change in sigma

		# two_sigma_m , delta_sigma
		while ( abs( (last_sigma - sigma) / sigma) > 1.0e-9 ) :
				two_sigma_m = 2 * sigma1 + sigma

				delta_sigma = B * math.sin(sigma) * ( math.cos(two_sigma_m) \
						+ (B/4) * (math.cos(sigma) * \
						(-1 + 2 * math.pow( math.cos(two_sigma_m), 2 ) -  \
						(B/6) * math.cos(two_sigma_m) * \
						(-3 + 4 * math.pow(math.sin(sigma), 2 )) *  \
						(-3 + 4 * math.pow( math.cos (two_sigma_m), 2 ))))) \

				last_sigma = sigma
				sigma = (s / (b * A)) + delta_sigma

		latitude2 = math.atan2 ( (math.sin(U1) * math.cos(sigma) + math.cos(U1) * math.sin(sigma) * math.cos(alpha1To2) ), \
				((1-f) * math.sqrt( math.pow(Sinalpha, 2) +  \
				pow(math.sin(U1) * math.sin(sigma) - math.cos(U1) * math.cos(sigma) * math.cos(alpha1To2), 2))))

		lembda = math.atan2( (math.sin(sigma) * math.sin(alpha1To2 )), (math.cos(U1) * math.cos(sigma) -  \
				math.sin(U1) *  math.sin(sigma) * math.cos(alpha1To2)))

		C = (f/16) * cosalpha_sq * (4 + f * (4 - 3 * cosalpha_sq ))

		omega = lembda - (1-C) * f * Sinalpha *  \
				(sigma + C * math.sin(sigma) * (math.cos(two_sigma_m) + \
				C * math.cos(sigma) * (-1 + 2 * math.pow(math.cos(two_sigma_m),2) )))

		longitude2 = longitude1 + omega

		alpha21 = math.atan2 ( Sinalpha, (-math.sin(U1) * math.sin(sigma) +  \
				math.cos(U1) * math.cos(sigma) * math.cos(alpha1To2)))

		alpha21 = alpha21 + two_pi / 2.0
		if ( alpha21 < 0.0 ) :
				alpha21 = alpha21 + two_pi
		if ( alpha21 > two_pi ) :
				alpha21 = alpha21 - two_pi

		latitude2	   = latitude2	   * 45.0 / piD4
		longitude2	= longitude2	* 45.0 / piD4
		alpha21	= alpha21	* 45.0 / piD4

		return latitude2,  longitude2,  alpha21

  # END of Vincenty's Direct formulae

###############################################################################
def est_dist(  latitude1,  longitude1,  latitude2,  longitude2 ) :
		"""

		Returns an estimate of the distance between two geographic points
		This is a quick and dirty vinc_dist
		which will generally estimate the distance to within 1%
		Returns distance in metres

		"""
		f = 1.0 / 298.257223563		# WGS84
		a = 6378137.0 			# metres

		piD4   = 0.785398163397

		latitude1	= latitude1 * piD4 / 45.0
		longitude1 = longitude1 * piD4 / 45.0
		latitude2	= latitude2 * piD4 / 45.0
		longitude2 = longitude2 * piD4 / 45.0

		c = math.cos((latitude2+latitude1)/2.0)

		return math.sqrt( pow(math.fabs(latitude2-latitude1), 2) + \
				pow(math.fabs(longitude2-longitude1)*c, 2) ) * a * ( 1.0 - f + f * c )
   	# END of rough estimate of the distance.

###############################################################################
def getPRJFromEPSG(EPSGCode):
	'''read through the SRID.csv file from Pyproj to find the correct PRJ string for a given EPSG code.  This is used to write out a sensible PRJ file alongside a shape file. '''
	localpath = os.path.dirname(os.path.realpath(__file__))
	sys.path.append(localpath)
	filename = os.path.join(localpath, "srid.csv")
	# filename = 'srid.csv'
	if os.path.isfile(filename):
		datafile = open(filename)
		for line in datafile:
			if EPSGCode in line[0:10]:
				prj = line.split(";")[1]
				#remove triple quotes
				prj = prj.replace('"""', '"')
				#remove double quotes
				prj = prj.replace('""', '"')
				#replace comma and quotes with comma
				prj = prj.replace(',"', ',')
				#replace squae bracket and quote with sqb
				prj = prj.replace(']"', ']')
				prj = prj.replace(',,,,', '')

				return prj
	return ""

###############################################################################
def loadProj(EPSGCode=0, wkt=""):
	'''load a pyproj object using the supplied code'''
	# wgs84=pyproj.Proj("+init=EPSG:4326") # LatLon with WGS84 datum used by GPS units and Google Earth
	#note: anaconda conda install has a bug when installing.  It is stupid and forgets to install the proj data folder.
	#to fix this, you need to
	#copy the data folder from c:\ggtools\python\pyproj to # C:\ProgramData\Anaconda3\Lib\site-packages\pyproj
	#rename the datadir.py to datadir.bak and then copy the datadir.py from the c:\ggtools\python\pyproj into the folder
	projection = None
	if len(WKT) > 0:
		try:
			projection = pyproj.Proj("EPSG:" + str(WKT))
		except:
			return None
		else:
			projection = None

	if EPSGCode > 0:
		try:
			# projection = pyproj.Proj("+init=EPSG:" + str(EPSGCode))
			projection = pyproj.Proj("EPSG:" + str(EPSGCode))
		except:
			return None
		else:
			projection = None

	return projection

###############################################################################
def	writePRJ(filename, EPSGCode='4326'):
	'''try and find a matching PRJ string from the Proj CSV file.  If we find one, write it as a PRJ file so the shape file opens nicely in GIS'''
	prjstring = getPRJFromEPSG(EPSGCode)
	prj = open(filename, 'w')
	if len(prjstring) > 0:
		prj.write(prjstring) # python will convert \n to os.linesep
	else:
		prj.write('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]') # python will convert \n to os.linesep
	prj.close() # you can omit in most cases as the destructor will call it

###############################################################################
def wkt2epsg(wkt=""):
	'''load a pyproj object using the WKT and return the EPSG code'''
	epsg = 0
	if len(wkt) > 0:
		try:
			crs = pyproj.CRS(wkt)
			epsg = crs.to_epsg(min_confidence=25)
		except:
			return None
		else:
			return epsg
	return epsg

##########################################################################################################################
class geodesy:
	'''a simple helper class to hold a pyproj geodesy object so we can transform with ease'''
	def __init__(self, EPSGCode = "", wkt=""):

		#clear the GDAL paths to proj as they break the pyproj libraries
		os.environ["PROJ_LIB"]  = ""
		os.environ["GDAL_DATA"] = ""

		self.EPSGCode = EPSGCode
		self.projection = None
		self.loadProj(EPSGCode, wkt)

	###############################################################################
	def wkt2epsg(self, wkt=""):
		'''load a pyproj object using the WKT and return the EPSG code'''
		if len(wkt) > 0:
			try:
				crs = pyproj.CRS(wkt)
				epsg = crs.to_epsg(min_confidence=25)
			except:
				return None
			else:
				return None
		return epsg
	
	###############################################################################
	def loadProj(self, EPSGCode=0, wkt=""):
		'''load a pyproj object using the supplied code'''
		# wgs84=pyproj.Proj("+init=EPSG:4326") # LatLon with WGS84 datum used by GPS units and Google Earth
		#note: anaconda conda install has a bug when installing.  It is stupid and forgets to install the proj data folder.
		#to fix this, you need to
		#copy the data folder from c:\ggtools\python\pyproj to # C:\ProgramData\Anaconda3\Lib\site-packages\pyproj
		#rename the datadir.py to datadir.bak and then copy the datadir.py from the c:\ggtools\python\pyproj into the folder
		projection = None

		if len(wkt) > 0:
			try:
				projection = pyproj.Proj(pyproj.CRS(wkt))
			except:
				return None
			else:
				projection = None

		if len(EPSGCode) > 0:
			try:
				# projection = pyproj.Proj("+init=EPSG:" + str(EPSGCode))
				projection = pyproj.Proj("EPSG:" + str(EPSGCode))
				
			except:
				return None

		self.projection = projection

		return projection

	###############################################################################
	def convertToGrid(self, longitude, latitude):
		'''convert from longitude, latitude to a projected easting, northing'''
		if int(self.EPSGCode) == 4326:
			return longitude, latitude

		if self.projection is None:
			return longitude, latitude

		# if self.projection.crs.is_projected == True:
		x,y = self.projection(float(longitude),float(latitude))
		return x,y

	###############################################################################
	def convertToGeographicals(self, easting, northing):
		'''convert from East, North to longitude, latitude'''
		if self.projection is not None:
			x,y = self.projection(float(easting),float(northing), inverse=True)
			return x,y
		else:
			return easting, northing



# Test driver

if __name__ == "__main__" :
		main()

#--------------------------------------------------------------------------
# Notes:
#
# * "The inverse formulae may give no solution over a line
# 	between two nearly antipodal points. This will occur when
# 	lembda ... is greater than pi in absolute value". (Vincenty, 1975)
#
# * In Vincenty (1975) L is used for the difference in longitude,
# 	however for consistency with other formulae in this Manual,
# 	omega is used here.
#
# * Variables specific to Vincenty's formulae are shown below,
# 	others common throughout the manual are shown in the Glossary.
#
#
# alpha = Azimuth of the geodesic at the equator
# U = Reduced latitude
# lembda = Difference in longitude on an auxiliary sphere (longitude1 & longitude2
# 		are the geodetic longitudes of points 1 & 2)
# sigma = Angular distance on a sphere, from point 1 to point 2
# sigma1 = Angular distance on a sphere, from the equator to point 1
# sigma2 = Angular distance on a sphere, from the equator to point 2
# sigma_m = Angular distance on a sphere, from the equator to the
# 		midpoint of the line from point 1 to point 2
# u, A, B, C = Internal variables
#
#
# Sample Data
#
# Flinders Peak
# -37 57'03.72030"
# 144 25'29.52440"
# Buninyong
# -37 39'10.15610"
# 143 55'35.38390"
# Ellipsoidal Distance
# 54,972.271 m
#
# Forward Azimuth
# 306 52'05.37"
#
# Reverse Azimuth
# 127 10'25.07"
#
#
