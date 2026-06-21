import io
import pyall

nbeams = 6
d = object.__new__(pyall.D_depth)
d.stx = 2
d.typeofdatagram = 'D'
d.emmodel = 300            # < 700 -> '=H3h2H2BbB'
d.recorddate = 20140104
d.time = pyall.to_timestamp(pyall.to_datetime(20140104, 3600.0))
d.counter = 7
d.serialnumber = 999
d.heading = 123.45
d.soundspeedattransducer = 1500.0
d.transducerdepth = 4.20
d.maxbeams = nbeams
d.nbeams = nbeams
d.zresolution = 0.01
d.xyresolution = 0.01
d.samplefrequency = 1
d.rangemultiplier = 1
d.depth = [10.00, 20.50, 100.25, 5.10, 0.00, 600.99]
d.acrosstrackdistance = [-50.00, 50.00, -100.10, 0.00, 12.34, -7.77]
d.alongtrackdistance = [1.00, -1.00, 2.50, -2.50, 0.00, 3.33]
d.beamdepressionangle = [10.00, -10.00, 20.00, -20.00, 0.00, 5.00]
d.beamazmuthangle = [0.00, 90.00, 180.00, 270.00, 359.00, 45.00]
d.range = [100.00, 200.00, 300.00, 50.00, 10.00, 600.00]
d.qualityfactor = [5, 2, 4, 1, 0, 7]
d.lengthofdetectionwindow = [3, 3, 4, 2, 1, 5]
d.reflectivity = [-1.00, -0.50, 0.00, 1.00, 1.20, -1.20]   # packed as signed byte *100
d.beamnumber = [0, 1, 2, 3, 4, 5]

encoded = d.encode()
buf = io.BytesIO(encoded)
d2 = pyall.D_depth(buf, len(encoded))
d2.read()

def close(a, b, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))

ok = (d2.nbeams == nbeams
      and close(d2.depth, d.depth)
      and close(d2.acrosstrackdistance, d.acrosstrackdistance)
      and close(d2.alongtrackdistance, d.alongtrackdistance)
      and close(d2.beamdepressionangle, d.beamdepressionangle)
      and close(d2.beamazmuthangle, d.beamazmuthangle)
      and close(d2.range, d.range)
      and list(d2.qualityfactor) == d.qualityfactor
      and list(d2.lengthofdetectionwindow) == d.lengthofdetectionwindow
      and close(d2.reflectivity, d.reflectivity)
      and list(d2.beamnumber) == d.beamnumber
      and d2.etx == 3)
print("D round-trip ok:", ok)
if not ok:
    print("nbeams", d2.nbeams, "etx", d2.etx)
    print("depth ", d2.depth)
    print("across", d2.acrosstrackdistance)
    print("refl  ", d2.reflectivity)
