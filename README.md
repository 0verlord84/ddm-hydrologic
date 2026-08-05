# DDM HydroLogic

**From DEM to model-ready catchments in QGIS.**

**v2.1 now features:**
1. Export to URBS (.vec routing file and .csv catchment data file) - Credits: Callan Shonrock
2. Multiple outlet lines can be drawn for multi-outlet models
3. Every model export has got companion shapefiles: subareas, centroids, entry points, nodal links and streams

**v2.0 now features:**
1. Retro compatibility with QGIS 3.22 LTR (tested on v3.22.16)
2. Faster sub catchment processing and selection

**INTRO**

Setting up a hydrologic model usually means a few hours of GIS prep work before the modelling even starts. DDM HydroLogic condenses some of the initial steps into one interactive session: it traces D8 flow paths from a DEM, ranks them by Strahler order, and lets you pick the relevant drainage. Draw an outlet line, set a minimum subcatchment size, and DDM HydroLogic cuts the catchment into dissolved subcatchment polygons — then you have choices of exporting as:

- **GeoPackage** containing flow path and subcatchment vectors,
- **RORB** `.catg` GE-ready catchment file,
- **WBNM** `.wbn` runfile,
- **XP-RAFTS** `.xpx` exchange file,
- **URBS** `.vec` and `.csv`,
- or some of the most popular **TUFLOW** xxx_R.shp.

It is important to note that all hydrological/hydraulic choices, such as rainfall, losses, Manning's coefficients, subcatchment types and slope, impervious fractions, etc.. have been deliberately left blank or default values.

Current version: **2.2** · QGIS 3.22 LTR and 4.x

## Workflow

1. Select a DEM lodaded in your instance. NOTE: A 5m DEM is a good sweet spot.
2. Choose the display flow-path accumulation threshold. The default is `10,000` (the lower the value the more effort & time).
3. Create a mask polygon (recommended) to confine the processing. Left-click to digitise, right-click to finish.
4. Press **Compute** to build temporary Strahler-ordered flow paths. If the mask is outside the DEM, the plugin shows a caution prompt. If NoData cells are detected in the active analysis domain, the areas are flagged as problematic.
5. Click flow paths to highlight upstream contributing subcatchment. CTRL + left-click to de-select an area.
6. (Optional) Draw an outlet line -> Recommended for hydrological modelling exports.
7. Enter a minimum subcatchment size in m2. (Default = 100,000 m²)
8. Press **Process subcatchments**. If successful, the plugin reports temporal layers with spatial representation of subcatchments and strahler-ordered flow lines.
9. Export vectors as a geopackage or straight into your favourite hydrological model and go from there.

## Outputs

- **Export flow paths and subcatchments to GeoPackage** writes same-order Strahler reaches and subcatchment polygons. The default filename is `DDM_HydroLogic_outputs_version_.gpkg`.
- **Export to RORB (.catg)** writes a first-pass RORBwin/RORB GE `.catg` file using a self-contained connected node-link writer. Sub-area nodes are placed on the main stream adjacent to the sub-area centroid, where the sub-area's rainfall-excess enters the channel network, and reach lengths are measured along the stream between adjacent nodes. The drawn outlet line is used to create the explicit RORB outlet node where available, and the export validates that every node drains to that outlet before writing (tested in RORB v6.52). The node and link geometry comes out in the companion shapefiles.
- **Export to WBNM 2025 (.wbn)** writes a first-pass WBNM runfile (see notes below).
- **Export to XP-RAFTS (.xpx)** writes a first-pass XP-RAFTS exchange file (see notes below).
- **Export TUFLOW files (.shp)** writes TUFLOW regions shp into a chosen folder. The final catchment will be included in the scaffoldings of the following: 2d_code, 2d_loc, 2d_rf, 2d_po, 2d_mat, 2d_qnl and 2d_soil.
- **Export to URBS (.vec/.csv)** writes a URBS routing vector file and catchment data file into a chosen folder (see notes below).

## RORB 6.52 export notes

The RORB export method outputs a .catg file that can be ingested straight into RORB
Graphical Editor (GE). Most of the parameters have been deliberately left either balnk
or with default values.

## WBNM 2025 export notes

The WBNM exporter writes a scaffold runfile, not a finished model. It fills in
only what the GIS can supply: the subarea topology, area in hectares, the
catchment/outlet coordinates and EPSG, and which subareas need a natural stream
segment. The file is laid out to the WBNM2023 runfile structure — exactly eight
preamble lines, two blank lines between blocks, 12-character fixed fields, and the
downstream subarea name in column 62 of each topology row — so it opens cleanly in
WBNM and it's GUI tools.

Rainfall is written as a single placeholder storm of zero depth, and the
local/outlet structure blocks are empty. Open the runfile in WBNM, replace the
rainfall, losses, imperviousness and structures with real values, and run
WBNMCHCK/WBNMSORT before relying on any results.

## XP-RAFTS export notes

DDM HydroLogic writes one RAFTS node per subcatchment (named, with easting/northing
captured from the geometry), one link per drainage connection, and the sub-area area
in hectares. Each node carries the five RAFTS sub-area slots, with slot 0 holding
the real sub-area and slots 1–4 as inert placeholders. Manning's coeff, sub-area slope, channel routing, losses and storms are written
as defaults — It's highly recommended to review them in XP-RAFTS before running. No design storms are
selected, so the imported model shows only geometries.

## TUFLOW export notes

DDM HydroLogic dissolves the processed subcatchments into a topologically valid
catchment polygon and writes it into each shapefile retaining the same CRS of the
source DEM. Draw more than one outlet line and you get one catchment per outlet,
numbered in the order the lines were drawn: Rain_001, Rain_002 and so on in the
2d_rf layer, Region_001, Region_002 in 2d_loc, and Code 1 on every 2d_code region:

The filenames use the `s1_s2_e1_e2_e3_EXG_001` scenario/event placeholder name.
Field names, types, widths and precisions follow the TUFLOW 2026.0.0 data formats.

## URBS export notes

DDM HydroLogic writes two UBRS input files into a chosen folder: a routing vector
file (`URBS_RoutingFile.vec`) and a catchment data file (`URBS_SubcatFile.csv`).
The routing file lists the subareas as RAIN / ADD RAIN / ROUTE THRU commands with
STORE. / GET. branch markers, and multiple outlets are supported. Reach lengths (L)
and channel slopes (Sc) come from each subarea's main channel; the catchment slope
(CS) is the equal-area slope from the subarea high point down to its outlet.
Land-use fractions (U, UF, I) are read from FracUrban/FracForest/FracImp fields if
the subcatchment layer has them, otherwise written as zero. Losses and model
parameters are left as defaults to complete in URBS.

## Companion GIS files

Every model export also writes five shapefiles into a folder of your choosing, in
the CRS of the DEM and named after the model file:

| File | Holds |
| --- | --- |
| `<Model>_Subareas.shp` | the subcathment polygons |
| `<Model>_Centroids.shp` | the subarea centroids |
| `<Model>_EntryPoints.shp` | the point on each subarea's main stream where its rainfall-excess enters the channel network |
| `<Model>_NodalLinks.shp` | the routing topology, `From_ID` to `To_ID` |
| `<Model>_Streams.shp` | the stream network, dissolved by Strahler order within each subarea |

Each layer carries `Model_ID`, the subarea label exactly as it appears in the model
file (`1`, `2`, `3` for URBS, `S001` for WBNM and XP-RAFTS, the node number for
RORB), so a node in the `.catg`, `.vec`, `.csv`, `.wbn` or `.xpx` can be found on
the map and the other way round. The files are loaded into their onw QGIS group
after each export.

## Scripting maintenance notes

- The RORB, WBNM, XP-RAFTS, TUFLOW and URBS exporters live in their own modules; the main just calls them.
- Startup and runtime failures report whether a Python module or a plugin file is missing, so hopefully it's clear what to install.
- WBNM export reads the engine's flow-accumulation values whether they are stored in a dict or a NumPy array.
