import pandas as pd
import geopandas as gpd

####################################################################################
# NOTE: uncleaned bus shapefile is taken from Bo's repo:
# https://github.com/boyuan276/NYgrid-python/tree/main/data/gis
# Original source unclear!
####################################################################################


# Create the bus shapefile, with some other helpful info
df_busprop = pd.read_csv("../bus_prop_boyuan.csv")
df_npcc = pd.read_csv("../npcc_new.csv")
gdf_bus = gpd.read_file("./Bus.shp")

# Merge
gdf = pd.merge(
    gdf_bus[["busIdx", "xcoord", "ycoord"]].rename(
        columns={"busIdx": "bus_id", "xcoord": "lon", "ycoord": "lat"}
    ),
    df_busprop[["BUS_I", "BUS_ZONE"]].rename(
        columns={"BUS_I": "bus_id", "BUS_ZONE": "zone"}
    ),
    how="inner",
    left_on="bus_id",
    right_on="bus_id",
)

# Add bus geometry
gdf["geometry"] = gpd.points_from_xy(gdf["lon"], gdf["lat"])
gdf = gpd.GeoDataFrame(gdf)
gdf.crs = gdf_bus.crs

# Store
gdf.to_file("./Bus_clean.shp")
