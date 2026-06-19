import os
import sys
import multiprocessing
import ctypes
import logging
from datetime import datetime, timedelta


###############################################################################
def	log(msg, error = False, printmsg=True):
		if printmsg:
			print (msg)
		if error == False:
			logging.info(msg)
		else:
			logging.error(msg)

########################################
def mpresult(msg):
	# print (msg)
	# g_procprogress.increment_progress(os.path.basename(msg))
	g_procprogress.increment_progress()

###############################################################################
def getcpucount(requestedcpu):
	'''control how many CPU's we use for multi processing'''
	try:
		requestedcpu = int(requestedcpu)
	except (TypeError, ValueError):
		requestedcpu = 0

	if requestedcpu <= 0:
		requestedcpu = multiprocessing.cpu_count()
		availablememoryingigs = get_available_memory_gb()
		if availablememoryingigs is not None:
			requiredgigspercpu = 4
			maxcpu = max(1, int(availablememoryingigs / requiredgigspercpu))
			if requestedcpu > maxcpu:
				requestedcpu = maxcpu

	return max(1, int(requestedcpu))

###############################################################################
def get_available_memory_gb():
	'''return available physical memory in GB, if it can be determined.'''
	if hasattr(ctypes, "windll"):
		try:
			stat = MEMORYSTATUSEX()
			ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
			return stat.ullAvailPhys/(1024*1024*1024)
		except (AttributeError, OSError):
			return None

	try:
		pagesize = os.sysconf("SC_PAGE_SIZE")
		availpages = os.sysconf("SC_AVPHYS_PAGES")
		return (pagesize * availpages) / (1024 * 1024 * 1024)
	except (ValueError, OSError, AttributeError):
		return None

###############################################################################
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

	###############################################################################
	def __init__(self):
		# have to initialize this to the size of MEMORYSTATUSEX
		self.dwLength = ctypes.sizeof(self)
		super(MEMORYSTATUSEX, self).__init__()

###############################################################################
class CPROGRESS(object):
	'''thread safe class to display progress in command window when in multiprocess mode'''
	# procprogress = CPROGRESS(1000)
	# for i in range(1000):
	# 	time.sleep(0.01)
	# 	procprogress.increment_progress("test", i)

	###########################################################################
	def __init__(self, maxcount=100):
		self.length = 20 # modify this to change the length
		self.maxcount = max(maxcount,1)
		# self.progress = 0
		self.stime = datetime.now()
		self.value = 0
		self.msg = "Progress:"

	###########################################################################
	def setmaximum(self, value, current=0):
		self.maxcount = value
		self.value = current
		self.stime = datetime.now()

	###########################################################################
	def increment_progress(self, msg="", value=0):
		
		if len(str(msg)) > 0:
			self.msg = msg

		if value == 0:
			self.value = self.value + 1
		else:
			self.value = value

		self.maxcount = max(self.maxcount,1)
		progress = self.value/self.maxcount
		
		# print(value)
		secondsconsumed = (datetime.now() - self.stime).total_seconds()
		secondsperitem = secondsconsumed / max(self.value,1)
		secondsremaining = int((self.maxcount - self.value) * secondsperitem)
		timeremaining = str(timedelta(seconds=secondsremaining))
		block = int(round(self.length*progress))
		msg = "\r{0}: [{1}] {2:2.2f}% Remaining: {3}".format(self.msg, "#"*block + "-"*(self.length-block), round(progress*100, 2), timeremaining)
		if progress >= 1: msg += " DONE\r\n"
		sys.stdout.write(msg)
		sys.stdout.flush()

	###########################################################################
	def complete(self, msg):
		length = 20 # modify this to change the length
		progress = 1
		block = int(round(length*progress))
		msg = "\r{0}: [{1}] {2}%".format(msg, "#"*block + "-"*(length-block), round(progress*100, 2))
		if progress >= 1: msg += " DONE\r\n"
		sys.stdout.write(msg)
		sys.stdout.flush()


#class used to display the progress
g_procprogress = CPROGRESS(0)
