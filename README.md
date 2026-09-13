# DDM HydroLogic

**From DEM to model-ready catchments in QGIS.**

**v2.3 now features:**
1. Breakdown window - a table of every subcatchment (ID, areas, upstream area, slope) with three ways to re-process them: target total, minimum area in pixel/m2/km2/ha, or Strahler confluence order.
2. Each section of the workflow is greyed out until its inputs are ready, so the steps can only be done in order.
3. RORB - the companion GIS files now report the same sub-area numbers as the .catg file.

**v2.2:**
1. RORB - The .catg file is now inheriting the Slope of Channel instead of a dummy value. 
2. XP-RAFTS - Slope of Channel was converted from a hardcoded value of 0.70 to catchment slope (%) per subarea.
3. Squashed extra bugs.

**v2.1:**
1. Export to URBS (.vec routing file and .csv catchment data file) - Credits: Callan Shonrock
2. Multiple outlet lines can be drawn for multi-outlet models
3. Every model export has got companion shapefiles: subareas, centroids, entry points, nodal links and streams

**v2.0:**
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

It is important to note that all hydrological/hydraulic choices, such as rainfall, losses, Manning's coefficients, subcatchment types, impervious fractions, etc.. have been deliberately left blank or default values. Anything the DEM can measure - sub-area areas, reach lengths and slopes - is calculated and written into the model files.

Current version: **2.3** · QGIS 3.22 LTR and 4.x

## Workflow

1. Select a DEM lodaded in your instance. NOTE: A 5m DEM is a good sweet spot.
2. Choose the display flow-path accumulation threshold. The default is `10,000` (the lower the value the more effort & time).
3. Create a mask polygon (recommended) to confine the processing. Left-click to digitise, right-click to finish.
4. Press **Compute** to build temporary Strahler-ordered flow paths. If the mask is outside the DEM, the plugin shows a caution prompt. If NoData cells are detected in the active analysis domain, the areas are flagged as problematic.
5. Click flow paths to highlight upstream contributing subcatchment. CTRL + left-click to de-select an area.
6. (Optional) Draw an outlet line -> Recommended for hydrological modelling exports.
7. Enter a minimum subcatchment size in m2. (Default = 100,000 m²)
8. Press **Process subcatchments**. If successful, the plugin reports temporal layers with spatial representation of subcatchments and strahler-ordered flow lines.
9. (Optional) Press **Breakdown** to review the subcatchments in a table and re-process them by target total, by area or by Strahler order until the breakdown suits the model.
10. Export vectors as a geopackage or straight into your favourite hydrological model and go from there.

## Outputs

- **Export flow paths and subcatchments to GeoPackage** writes same-order Strahler reaches and subcatchment polygons. The default filename is `DDM_HydroLogic_outputs_version_.gpkg`.
- **Export to RORB (.catg)** writes a first-pass RORBwin/RORB GE `.catg` file using a self-contained connected node-link writer. Sub-area nodes are placed on the main stream adjacent to the sub-area centroid, where the sub-area's rainfall-excess enters the channel network, and reach lengths are measured along the stream between adjacent nodes. The drawn outlet line is used to create the explicit RORB outlet node where available, and the export validates that every node drains to that outlet before writing (tested in RORB v6.52). The node and link geometry comes out in the companion shapefiles.
- **Export to WBNM 2025 (.wbn)** writes a first-pass WBNM runfile (see notes below).
- **Export to XP-RAFTS (.xpx)** writes a first-pass XP-RAFTS exchange file (see notes below).
- **Export TUFLOW files (.shp)** writes TUFLOW regions shp into a chosen folder. The final catchment will be included in the scaffoldings of the following: 2d_code, 2d_loc, 2d_rf, 2d_po, 2d_mat, 2d_qnl and 2d_soil.
- **Export to URBS (.vec/.csv)** writes a URBS routing vector file and catchment data file into a chosen folder (see notes below).

## Breakdown window

**Breakdown** opens a table of every processed subcatchment: the QGIS-side ID, the
area in m2, km2 and ha, a Label you can type into, the total area reporting to the
subcatchment from upstream, and the equal-area slope of its main flowpath. Click a
row and that subcatchment is outlined on the canvas. The columns sort on a header
click and the table can be exported with **Export as CSV**.

The Processing frame at the top re-processes the whole breakdown three ways:

- **Set target total** aims at a number of subcatchments. Confluences force
  boundaries of their own and the area threshold moves the count in steps, so the
  exact number is usually out of reach - the nearest achievable breakdown is used
  and a message says what came out.
- **Set by area** takes a number and a unit (pixel, m2, km2 or ha) and makes each
  subcatchment that size or larger, as closely as the flow paths allow.
- **Set by Strahler order** cuts at stream confluences of the order entered or
  above. Strahler order only exists on the flow paths the step-2 accumulation
  threshold displays, so the window shows the highest order available.

**Re-process subcatchments** redraws the breakdown into its own temporary layer and
leaves the original one on the map, so the two can be compared. **Close** throws
the re-processing away and puts the original back. **Confirm new subcatchments**
keeps the new set for the exports and asks whether to keep the original layer too.
Typed labels belong to the subcatchments on screen, so re-processing clears them
after asking.

The ID counts outwards from the outlet: 1 is an outlet subcatchment and the number
grows with distance upstream, each catchment numbered through before the next one
starts. It is written to the temporary layer and into the companion shapefiles as
`DDM_ID`, with the Label beside it. It is a GIS handle for finding a subcatchment
on the map - the model files keep their own numbering, because RORB fixes sub-area
identity by the order its control vector visits them and cannot be renumbered.

## RORB 6.52 export notes

The RORB export method outputs a .catg file that can be ingested straight into RORB
Graphical Editor (GE). Some of the parameters have been deliberately left either balnk
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
the real sub-area and slots 1–4 as inert placeholders. The sub-area slope (SC) is the equal-area catchment
slope as a percentage, taken from the DEM. Manning's coeff, channel routing, losses and storms are written
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

Two slope fields are written, both equal-area slopes in m/m: `CatSlope` runs from
the subarea's high point down to its outlet (the whole catchment, hillslope
included), while `Slope_m_m` follows the main channel only. RORB reach slopes come
from the channel one and XP-RAFTS `SC` and URBS `CS` come from the catchment one,
each converted to the units that model expects.

Each layer also carries `DDM_ID` and `Label` from the Breakdown window, and
`Model_ID`, the subarea label exactly as it appears in the model
file (`1`, `2`, `3` for URBS, `S001` for WBNM and XP-RAFTS, the node number for
RORB), so a node in the `.catg`, `.vec`, `.csv`, `.wbn` or `.xpx` can be found on
the map and the other way round. The files are loaded into their onw QGIS group
after each export.

## Scripting maintenance notes

- The RORB, WBNM, XP-RAFTS, TUFLOW and URBS exporters live in their own modules; the main just calls them.
- Startup and runtime failures report whether a Python module or a plugin file is missing, so hopefully it's clear what to install.
- WBNM export reads the engine's flow-accumulation values whether they are stored in a dict or a NumPy array.
