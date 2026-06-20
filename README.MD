# pyall

`pyall` is a Python toolkit for reading **Kongsberg `.all` multibeam echosounder files**. It can:

- decode every common datagram (position, attitude, depth, runtime, sound velocity, seabed image, ...);
- build georeferenced **bathymetric point clouds** (east, north, depth, quality, reflectivity);
- rasterise soundings or reflectivity into **GeoTIFFs** (float, colour ramp or greyscale);
- expose all of the above to AI assistants through a **Model Context Protocol (MCP) server**.

- created: August 2016
- by: paul.kennedy@guardiangeomatics.com
- based on the Kongsberg ALL file specification, Revision R (October 2013)
- refreshed for Python 3.14 (originally developed for Python 3.4)

---

## Contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Using the `pyall` module](#using-the-pyall-module)
  - [Low-level datagram reader](#low-level-datagram-reader)
  - [Point clouds and GeoTIFFs](#point-clouds-and-geotiffs)
  - [Loader helpers](#loader-helpers)
- [Command line tools](#command-line-tools)
- [The MCP server](#the-mcp-server)
  - [Running the server](#running-the-server)
  - [Configuring an MCP client](#configuring-an-mcp-client)
  - [Available tools](#available-tools)
  - [Examples of the MCP in action](#examples-of-the-mcp-in-action)
- [Output files](#output-files)

---

## Installation

Requires **Python 3.10+** (developed/tested against 3.14).

```powershell
# from the repository root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` pulls in `numpy`, `rasterio`, `pyproj`, `scipy`, `pyshp`, `plotly` and `mcp`.

A sample data file is included at `sample/0253_20140104_000401_Duke.all`.

---

## Quick start

```powershell
# summarise a file (datagram counts, position, suitable EPSG)
.\.venv\Scripts\python.exe all2points.py -i sample\0253_20140104_000401_Duke.all -info

# create a point cloud CSV + GeoTIFF for every .all file in a folder
.\.venv\Scripts\python.exe all2points.py -i sample
```

```python
# or from python
import pyall

info = pyall.getfileinfo(r"sample\0253_20140104_000401_Duke.all")
print(info["epsg"], info["datagramcounts"])

tif = pyall.depthtotif(r"sample\0253_20140104_000401_Duke.all", value="depth", colour="jeca")
print("wrote", tif)
```

---

## Using the `pyall` module

### Low-level datagram reader

The `allreader` class streams through a file. `readdatagram()` returns the datagram
type code and a class instance; you then call `.read()` on the records you care
about, which keeps scanning fast.

```python
import pyall

r = pyall.allreader(r"sample\0253_20140104_000401_Duke.all")

while r.moredata():
    typeofdatagram, datagram = r.readdatagram()

    if typeofdatagram == 'P':            # position
        datagram.read()
        print("Lat: %.5f Lon: %.5f" % (datagram.latitude, datagram.longitude))

    if typeofdatagram == 'X':            # XYZ depth
        datagram.read()
        nadir = int(datagram.nbeams / 2)
        print("Nadir depth: %.2f m" % datagram.depth[nadir])

r.rewind()
r.close()
```

Common datagram codes: `P` position, `A`/`n` attitude, `C` clock, `D`/`X` depth,
`N`/`f` raw range & travel time, `R` runtime parameters, `U` sound velocity profile,
`Y` seabed image, `I`/`i` installation, `h` height, `3` extra parameters.

### Point clouds and GeoTIFFs

```python
import pyall

filename = r"sample\0253_20140104_000401_Duke.all"
params = {'epsg': '0', 'odir': 'out', 'debug': '-1', 'verbose': False}

# build a point cloud (east, north, depth, quality, reflectivity)
cloud = pyall.loaddata(filename, params)
print(len(cloud.xarr), "points")
print(cloud.xarr[0], cloud.yarr[0], cloud.zarr[0], cloud.qarr[0], cloud.rarr[0])

# write a CSV (_R.txt) + float GeoTIFF in one step
geotiff = pyall.all2point(filename, params)

# or grid directly with colour / reflectivity options
shaded = pyall.depthtotif(filename, resolution=2, value='reflectivity', colour='grey')
```

`epsg='0'` auto-detects a suitable projected CRS from the file's first position.

### Loader helpers

These functions read a whole file and return ready-to-use Python/NumPy data:

| Function | Returns |
| --- | --- |
| `getfileinfo(file)` | datagram counts, position, file size, suitable EPSG |
| `getsuitableepsg(file)` | a projected EPSG code for the survey area |
| `loaddata(file, params)` | a point cloud object (`xarr`, `yarr`, `zarr`, `qarr`, `rarr`) |
| `loadpositions(file)` | position records |
| `loadattitude(file)` | attitude array `[timestamp, roll, pitch, heave, heading]` |
| `loadclock(file)` | clock records |
| `loadheight(file)` | height records |
| `loadsoundvelocityprofiles(file)` | SVP datagrams |
| `loadsurfacesoundspeed(file)` | surface sound speed datagrams |
| `loadruntimeparameters(file)` | decoded runtime settings |
| `loadtraveltime(file, max)` | raw range / beam-angle records |
| `loadinstallationparameters(file)` | installation offsets and serials |
| `loaddepth(file, maxpings)` | per-beam soundings |
| `loadseabedimage(file, maxpings)` | seabed image backscatter samples |
| `loadpustatus(file)` | PU status / sensor health records |

---

## Command line tools

`all2points.py` reads a file (or folder) and writes a point cloud CSV plus a GeoTIFF.

```powershell
.\.venv\Scripts\python.exe all2points.py -i <file-or-folder> [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `-i` | current folder | Input `.all` file or a folder of `.all` files |
| `-epsg` | `0` (auto) | Output EPSG code, e.g. `-epsg 32756` |
| `-odir` | timestamped folder | Output folder |
| `-debug` | `500` | Number of pings to process (`-1` = all) |
| `-verbose` | off | Verbose logging / extra supporting files |

If no `-i` is given it processes every `.all` file in the current directory.

---

## The MCP server

`pyall_mcp.py` is a [Model Context Protocol](https://modelcontextprotocol.io) server
built with `FastMCP`. It exposes the pyall functionality as tools that an AI
assistant (Claude Desktop, VS Code, etc.) can call. It uses a thread pool
(`asyncio.to_thread`) for concurrency and depends only on the `pyall` module.

### Running the server

```powershell
.\.venv\Scripts\python.exe pyall_mcp.py
```

The server speaks the **stdio** transport, so it is normally launched by an MCP
client rather than run by hand.

### Configuring an MCP client

Add an entry like this to your client's MCP configuration (paths are examples —
use the absolute paths to your venv and repo):

```json
{
  "mcpServers": {
    "pyall": {
      "command": "C:\\ggtools\\pyall\\.venv\\Scripts\\python.exe",
      "args": ["C:\\ggtools\\pyall\\pyall_mcp.py"]
    }
  }
}
```

### Available tools

**Processing / gridding**

| Tool | Purpose |
| --- | --- |
| `get_file_info` | Summarise a file (datagram counts, position, EPSG). |
| `depth_to_grid` | Grid depth or reflectivity to a GeoTIFF (float / colour / grey). |
| `all_to_tif` | Build a point cloud and rasterise to a float GeoTIFF (+ CSV). |
| `all_to_pointcloud` | Export the point cloud as a CSV only. |
| `batch_process` | Process every `.all` file in a folder concurrently. |

**Datagram record access**

| Tool | Record |
| --- | --- |
| `get_positions` | `P` position |
| `get_attitude` | `A` attitude |
| `get_clock` | `C` clock |
| `get_height` | `h` height |
| `get_sound_velocity_profiles` | `U` sound velocity profile |
| `get_surface_sound_speed` | `G` surface sound speed |
| `get_runtime_parameters` | `R` runtime parameters |
| `get_travel_time` | `N` raw range & beam angle |
| `get_installation_parameters` | `I` installation |
| `get_depth` | `X`/`D` per-beam soundings |
| `get_seabed_image` | `Y` seabed image backscatter |
| `get_pu_status` | `1` PU status / sensor health |

Large records accept `max_records` / `max_pings` / `max_points` arguments so
responses stay a manageable size; results report a `truncated` flag and the true
`count`.

### Examples of the MCP in action

Once the server is connected, you interact with it in natural language and the
assistant calls the tools for you. A few illustrative prompts and the tool calls
they map to:

> **"Summarise `sample/0253_20140104_000401_Duke.all` — where was it collected and what EPSG should I use?"**

calls `get_file_info` →

```json
{
  "filename": "sample/0253_20140104_000401_Duke.all",
  "filesize": 12648448,
  "approxlongitude": 174.78,
  "approxlatitude": -36.42,
  "epsg": "32760",
  "datagramcounts": { "P": 421, "A": 1683, "X": 842, "R": 5, "U": 2 }
}
```

> **"Make a coloured depth GeoTIFF of that file at 2 m resolution."**

calls `depth_to_grid` with `value="depth"`, `colour="jeca"`, `resolution=2` →

```json
{
  "input_file": "sample/0253_20140104_000401_Duke.all",
  "value": "depth",
  "colour": "jeca",
  "resolution": 2,
  "geotiff": "sample/0253_..._Duke.all_depth_jeca.tif"
}
```

> **"Now grid the reflectivity in greyscale instead."**

calls `depth_to_grid` with `value="reflectivity"`, `colour="grey"`.

> **"Export the point cloud to CSV so I can load it in CloudCompare."**

calls `all_to_pointcloud` →

```json
{
  "input_file": "sample/0253_20140104_000401_Duke.all",
  "epsg": "32760",
  "output_dir": "sample/all2point_20260620-101500",
  "pointcloud_csv": "sample/all2point_.../0253_..._Duke.all_R.txt",
  "point_count": 215463
}
```

> **"Process the whole `sample` folder into GeoTIFFs, 4 files at a time."**

calls `batch_process` with `input_folder="sample"`, `max_concurrency=4` → a list of
per-file results, each with its `geotiff` path and any `error`.

> **"Check the clock stability and show me the first few runtime settings."**

calls `get_clock` and `get_runtime_parameters` and returns the decoded records
(PC vs external time / PPS, depth mode, filters, coverage, etc.).

You can quickly verify the tools are registered without a client:

```powershell
.\.venv\Scripts\python.exe -c "import asyncio, pyall_mcp; print(sorted(t.name for t in asyncio.run(pyall_mcp.mcp.list_tools())))"
```

---

## Output files

- `<file>_R.txt` — CSV point cloud: `east, north, depth, quality, reflectivity`.
- `<file>_..._depth.tif` / reflectivity tif — GeoTIFF raster (float, colour ramp or greyscale).
- `all2point_log.txt` — run log written into the output folder.

---

## Notes on data types

Kongsberg fields map to Python `struct` format characters as follows:

| ALL type | Bytes | `struct` |
| --- | --- | --- |
| signed char | 1 | `b` |
| unsigned char | 1 | `B` |
| signed short | 2 | `h` |
| unsigned short | 2 | `H` |
| DWORD (unsigned int) | 4 | `L` |
| char | 1 | `c` |
