import os, glob, time, traceback
import qcall

SRC = r"E:\sampledata\all"
OUT = os.path.join(SRC, "out")
os.makedirs(OUT, exist_ok=True)
files = sorted(glob.glob(os.path.join(SRC, "*.all")))
print("Backscatter v%s on %d files -> %s" % (qcall.__version__, len(files), OUT), flush=True)
t0 = time.time(); ok = fail = 0
for i, f in enumerate(files, 1):
    print("[%d/%d] %s" % (i, len(files), os.path.basename(f)), flush=True)
    try:
        b = qcall.backscattertotif(f, colour='grey', odir=OUT)
        ok += 1 if b else 0
    except Exception:
        fail += 1; print("   FAILED"); traceback.print_exc()
print("=" * 60, flush=True)
print("DONE: %d backscatter, %d failures in %.1f min" % (ok, fail, (time.time()-t0)/60.0), flush=True)
