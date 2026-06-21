import os
import math
import numpy as np
###############################################################################
def main():
    '''an example how to use the time series class.
    '''
    attitude = [[0.001,100],[2,200], [5,500], [10,1000]]
    tsRoll = cTimeSeries(attitude)
    print(tsRoll.getValueAt(6))
    # now make an interpolated time series.  this is useful if we need to export at a given interval.
    interval=1
    interpolatedroll = tsRoll.createinterpolatedseries(interval)
    print(interpolatedroll)

    # Example time series data with spikes
    times = np.linspace(0, 10, 100)
    values = np.sin(times) + np.random.normal(0, 0.1, 100)
    values[20] = 5  # Add a spike
    values[50] = -3  # Add another spike
    
    # Create a cTimeSeries object
    ts = cTimeSeries(times, values)

    # Save original values for plotting
    original_values = np.copy(ts.values)

    # Apply the spikefilter
    ts.spikefilter2()

    smooth = ts.smoothfilter(window_length=11, polyorder=2)

###############################################################################
class cTimeSeries:
    '''# how to use the time series class, a 2D list of time
    # attitude = [[1,100],[2,200], [5,500], [10,1000]]
    # tsRoll = cTimeSeries(attitude)
    # print(tsRoll.getValueAt(6))'''

###############################################################################
    def __init__(self, timeOrTimeValue, values=""):
        '''the time series requires a 2d series of [[timestamp, value],[timestamp, value]].  It then converts this into a numpy array ready for fast interpolation'''
        self.name = "2D time series"
        # user has passed 1 list with both time and values, so handle it
        if len(values) == 0:
                arr = np.array(timeOrTimeValue)
                #sort the list into ascending time order
                arr = arr[np.argsort(arr[:,0])]
                self.times = arr[:,0]
                self.values = arr[:,1]
        else:
            # user has passed 2 list with time and values, so handle it
            self.times = np.array(timeOrTimeValue)
            self.values = np.array(values)

###############################################################################
    def spikefilter2(self):
        '''use median filtering to detect spikes and interpolate to remove them'''
        # Determine a dynamic kernel size based on the length of the data
        kernel_size = max(3, min(11, len(self.values) // 10))
        if kernel_size % 2 == 0:
            kernel_size += 1  # Ensure kernel size is odd

        # Apply median filter
        filtered_values = signal.medfilt(self.values, kernel_size=kernel_size)
        
        # Calculate the median absolute deviation (MAD)
        mad = np.median(np.abs(self.values - np.median(self.values)))
        threshold = 3 * mad  # Use a robust threshold based on MAD
        
        # Identify spikes
        spikes = np.abs(self.values - filtered_values) > threshold
        
        # Interpolate to replace spikes
        self.values[spikes] = np.interp(self.times[spikes], self.times[~spikes], self.values[~spikes])

    ###############################################################################
    def spikefilterQC(self):
        # Save original values for plotting
        original_values = np.copy(self.values)
        self.spikefilter2()
        
        length = max(1, math.floor(len(self.values) / 10))
        smooth = self.smoothfilter(window_length=length, polyorder=2)
        pass
    
    ###############################################################################
    def smoothfilter(self, window_length=11, polyorder=2):
        '''use Savitzky-Golay filter to smooth the time series data'''
        result = signal.savgol_filter(self.values, window_length=window_length, polyorder=polyorder)
        return result

    ###############################################################################
    def getValueAt(self, timestamp):
        return np.interp(timestamp, self.times, self.values, left=None, right=None)

    ###############################################################################
    def createinterpolatedseries(self, interval=1):
        '''now make a new time series interpolated at the user required interval
        '''
        starttime = self.times[0]
        endtime = self.times[-1]

        # create an index of times for the required range.  
        ts = np.arange(starttime, endtime, interval)
        # we need to extend the end record as numpy arange does not include the last record
        ts = np.append(ts, endtime)
        # now interpolate quickly using numpy
        interpolatedValues = np.interp(ts, self.times, self.values, left=None, right=None)
        # put the answers into a new class, so we can use them
        interpolate_ts = cTimeSeries(ts, interpolatedValues)
        return interpolate_ts

    ###############################################################################
    def getNearestAt(self, timestamp):
        idx = np.searchsorted(self.times, timestamp, side="left")
        if idx > 0 and (idx == len(self.times) or math.fabs(timestamp - self.times[idx-1]) < math.fabs(timestamp - self.times[idx])):
            return self.times[idx-1], self.values[idx-1]
        else:
            return self.times[idx], self.values[idx]

###################################################################################################
###################################################################################################
if __name__ == "__main__":
        main()
###################################################################################################
###################################################################################################
