from glob import glob
from multiprocessing import Pool, cpu_count

import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr
import salem
import cartopy.crs as ccrs
from scipy.optimize import minimize_scalar
from scipy import interpolate
from python.utils import (
    nrel_wtk_path,
    merge_to_zones,
    project_path,
    fill_missing_zones,
    nearest_neighbor_lat_lon,
)


def read_all_wtk(
    keep_every=10,
    vars_to_keep=["wind_speed", "power"],
    nrel_wtk_path: str = nrel_wtk_path,
):
    """
    Read all WTK data from the given path.

    Parameters:
    -----------
    keep_every : int, optional
        Every nth WTK location to keep
    vars_to_keep : list, optional
        Variables to keep
    nrel_wtk_path : str, optional
        Path to the NREL WTK data

    Returns:
    --------
    df_all : pd.DataFrame
        DataFrame containing the merged data
    """
    wtk_files = glob(f"{nrel_wtk_path}/techno-economic/met_data/*.nc")

    df_all = []

    # Loop through all
    for file in wtk_files[::keep_every]:
        # Read
        ds = xr.open_dataset(file)

        # Subset to vars
        ds = ds[vars_to_keep]

        # Decode times
        start_time = pd.to_datetime(ds.attrs["start_time"], unit="s", origin="unix")
        time_index = pd.date_range(
            start=start_time, periods=len(ds["time"]), freq="5min"
        )

        # Convert to dataframe
        df = ds.to_dataframe().reset_index()

        # Decode times
        df["datetime"] = time_index.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="NaT"
        )
        df["datetime"] = df["datetime"].dt.tz_convert("UTC")

        # Add lon/lat
        df["wtk_lon"] = ds.attrs["longitude"]
        df["wtk_lat"] = ds.attrs["latitude"]

        # Resample to hourly
        df = df.set_index("datetime").resample("1h").mean()

        # Tidy
        df = df.drop(columns=["time"])
        df = df.rename(columns={"wind_speed": "ws_wtk", "power": "power_wtk"})

        # Append to all
        df_all.append(df)

    # Add zone info
    df_all = pd.concat(df_all)
    df_all = merge_to_zones(df_all, lat_name="wtk_lat", lon_name="wtk_lon")

    return df_all


def _read_wind_climate_data(args):
    """
    Function to help read and subset climate data in parallel.

    Parameters:
    -----------
    args : tuple
        Arguments
    file : str
        Path to the climate data file
    lat_name : str
        Name of the latitude variable
    lon_name : str
        Name of the longitude variable
    x_min : float
        Minimum longitude
    x_max : float
        Maximum longitude
    y_min : float
        Minimum latitude
    y_max : float
        Maximum latitude
    wind_vars : list
        Variables to keep
    use_salem : bool, optional
        Whether to use Salem to read the data (useful for WRF data like TGW)

    Returns:
    --------
    ds : xr.Dataset
        Dataset containing the climate data
    """
    # Unpack
    file, lat_name, lon_name, x_min, x_max, y_min, y_max, wind_vars, use_salem = args

    # Open
    if use_salem:
        ds = salem.open_wrf_dataset(file)
    else:
        ds = xr.open_dataset(file)

    # Subset
    ds = ds[wind_vars].sel(
        {lat_name: slice(y_min, y_max), lon_name: slice(x_min, x_max)}
    )

    # Return
    return ds.load()


def _select_wind_climate_data_point(args):
    """
    Function to help select climate data points in parallel.

    Parameters:
    -----------
    args : tuple
        Arguments
    ds : xr.Dataset
        Dataset containing the climate data
    lat_name : str
        Name of the latitude variable
    lon_name : str
        Name of the longitude variable
    time_name : str
        Name of the time variable
    lat : float
        Latitude of the desired point
    lon : float
        Longitude of the desired point
    curvilinear : bool
        Whether the climate data is on a curvilinear grid

    Returns:
    --------
    df : pd.DataFrame
        DataFrame containing the climate data
    """
    # Unpack
    ds, lat_name, lon_name, time_name, lat, lon, curvilinear = args

    # Select climate data point
    if curvilinear:
        ds_crs = ccrs.Projection(ds.pyproj_srs)
        x, y = ds_crs.transform_point(
            np.round(lon, 2), np.round(lat, 2), src_crs=ccrs.PlateCarree()
        )
    else:
        x, y = lon, lat

    ds_sel = ds.sel({lon_name: x, lat_name: y}, method="nearest")

    # Take only the wind data
    df = ds_sel.to_dataframe().reset_index()

    # Add info
    df["desired_lat"] = lat
    df["desired_lon"] = lon
    df["datetime"] = pd.to_datetime(df[time_name])
    df["datetime"] = df["datetime"].dt.tz_localize("UTC")
    df = df.drop(columns=time_name)

    # Return
    return df


def prepare_wind_data(
    climate_paths,
    wind_vars,
    compute_wind_speed=True,
    lat_name="lat",
    lon_name="lon",
    time_name="time",
    curvilinear=False,
    use_salem=True,
    parallel=False,
    stab_coef_file=None,
    coef_cols=["month", "hour", "ZONE"],
    sites="wtk",
    wtk_keep_every=10,
    min_lat=39,  # approx NYS
    max_lat=45,  # approx NYS
    min_lon=-80,  # approx NYS
    max_lon=-71,  # approx NYS
):
    """
    Gather input data for wind power generation.

    Parameters:
    -----------
    climate_paths : list
        List of climate data file paths
    wind_vars : list
        List of wind variables to extract
    compute_wind_speed : bool
        Whether to compute wind speed from directional wind vars (u, v)
    lat_name : str
        Name of the latitude variable in climate data
    lon_name : str
        Name of the longitude variable in climate data
    time_name : str
        Name of the time variable in climate data
    curvilinear : bool
        Whether the climate data is on a curvilinear grid
    use_salem : bool
        Whether to use Salem to read the data (useful for WRF data like TGW)
    parallel : bool
        Whether to run in parallel using multiprocessing
    stab_coef_file : str
        Path to the stability coefficients file
    sites : str
        Whether to use WTK sites or a custom set of lat/lon points
    wtk_keep_every : int
        Every nth WTK site to keep
    min_lat : float
    max_lat : float
    min_lon : float
    max_lon : float
        Subset climate data based on these bounds

    Returns:
    --------
    df : pd.DataFrame
        DataFrame containing climate data
    """
    # Get bounds for NYS
    if curvilinear:
        # Get CRS
        ds_tmp = salem.open_wrf_dataset(climate_paths[0])
        ds_crs = ccrs.Projection(ds_tmp.pyproj_srs)
        # Get bounds
        x_min, y_min = ds_crs.transform_point(
            min_lon, min_lat, src_crs=ccrs.PlateCarree()
        )
        x_max, y_max = ds_crs.transform_point(
            max_lon, max_lat, src_crs=ccrs.PlateCarree()
        )
    else:
        x_min, y_min = min_lon, min_lat
        x_max, y_max = max_lon, max_lat

    # Read in parallel
    if parallel:
        n_cores = cpu_count() - 1
        # Prepare args for each file
        args = [
            (file, lat_name, lon_name, x_min, x_max, y_min, y_max, wind_vars, use_salem)
            for file in np.sort(climate_paths)
        ]
        with Pool(processes=n_cores - 1) as pool:
            # Read climate data
            ds_all = pool.map(_read_wind_climate_data, args)
        ds = xr.concat(ds_all, dim="time")
    else:
        ds = []
        for file in np.sort(climate_paths):
            # Read climate data
            ds_tmp = _read_wind_climate_data(
                (
                    file,
                    lat_name,
                    lon_name,
                    x_min,
                    x_max,
                    y_min,
                    y_max,
                    wind_vars,
                    use_salem,
                )
            )

            # Append
            ds.append(ds_tmp)
        ds = xr.concat(ds, dim="time")

    # Subset to WTK data only
    ds = ds.sortby("time")
    assert len(ds.time) > 0, "No data found"

    # Get lat/lons from NYS wind sites
    if isinstance(sites, str) and sites == "wtk":
        df_wtk = read_all_wtk(keep_every=wtk_keep_every)
        latlons = df_wtk[["wtk_lat", "wtk_lon"]].value_counts().index.to_numpy()
    elif isinstance(sites, (list, np.ndarray)):
        latlons = sites
    else:
        raise ValueError(f"Invalid sites: {sites}")

    # Loop through WTK lat/lons
    if parallel:
        with Pool(processes=n_cores - 1) as pool:
            # Prepare args
            args = [
                (
                    ds,
                    lat_name,
                    lon_name,
                    time_name,
                    lat,
                    lon,
                    curvilinear,
                )
                for lat, lon in latlons
            ]
            # Select climate data point
            df_all = pool.map(_select_wind_climate_data_point, args)
    else:
        df_all = []
        for lat, lon in latlons:
            # Select climate data point
            df = _select_wind_climate_data_point(
                (
                    ds,
                    lat_name,
                    lon_name,
                    time_name,
                    lat,
                    lon,
                    curvilinear,
                )
            )
            # Append
            df_all.append(df)

    # Combine all
    df_all = pd.concat(df_all, ignore_index=True)

    # Merge
    if isinstance(sites, str) and sites == "wtk":
        df_all = pd.merge(
            df_all,
            df_wtk.reset_index(),
            right_on=["datetime", "wtk_lat", "wtk_lon"],
            left_on=["datetime", "desired_lat", "desired_lon"],
        )
    elif isinstance(sites, (list, np.ndarray)):
        df_all = pd.merge(
            df_all,
            pd.DataFrame(latlons, columns=["desired_lat", "desired_lon"]),
            on=["desired_lat", "desired_lon"],
        )

    # Drop duplicates
    df_all = (
        df_all.set_index(["desired_lat", "desired_lon", "datetime"])
        .sort_index()
        .reset_index()
    ).drop_duplicates()

    # Add datetime info
    df_all["month"] = df_all["datetime"].dt.month
    df_all["dayofyear"] = df_all["datetime"].dt.dayofyear
    df_all["hour"] = df_all["datetime"].dt.hour

    # Compute wind speed
    if compute_wind_speed:
        df_all["ws"] = np.sqrt(np.sum(df_all[wind_vars] ** 2, axis=1))

    # Add stability coefficients
    if stab_coef_file is not None:
        # Merge to zones
        df_all = merge_to_zones(df_all, lat_name="lat", lon_name="lon")

        # Get stability coefficients
        df_stab = pd.read_csv(stab_coef_file)
        df_stab = fill_missing_zones(df_stab)

        # Read coefficeints
        df_all = pd.merge(
            df_all,
            df_stab,
            on=coef_cols,
        )
        # Compute hubheight wind speed
        df_all["ws_hubheight"] = df_all["ws"] * (100 / 10) ** df_all["alpha"]

    # Return
    return df_all


def get_stability_coefficients(
    df,
    ws_climate_10m,
    ws_hubheight,
    groupby_cols=["month", "hour", "ZONE"],
):
    """
    Calculates stability coefficients from climate data.

    Parameters:
    -----------
    df : pd.DataFrame
        DataFrame containing climate data
    ws_climate : str
        Name of the climate wind speed variable
    ws_hubheight : float
        Hub height of the wind turbine
    lookup_cols : list
        Columns to use for the lookup table
    """

    # Objective function
    def _objective(alpha, df, ws_climate, ws_hubheight):
        # Inferred hubheight windspeed
        df["ws_climate_hubheight"] = df[ws_climate_10m] * (100 / 10) ** alpha

        # Calculate RMSE
        rmse = np.sqrt(np.mean((df["ws_climate_hubheight"] - df[ws_hubheight]) ** 2))

        # Return
        return rmse

    # Optimize for each group
    res = df.groupby(groupby_cols).apply(
        lambda x: minimize_scalar(
            _objective,
            bounds=(0.01, 1.0),
            args=(x, ws_climate_10m, ws_hubheight),
            method="bounded",
        ).x
    )

    # Return
    return pd.DataFrame(res).rename(columns={0: "alpha"})


##################################################################
# NREL Power Curves
# See Table 2 in https://docs.nrel.gov/docs/fy14osti/61714.pdf
##################################################################
df_power_curves = pd.read_csv(
    f"{project_path}/data/wind/nrel_power_curves.csv",
)


def create_interpolation_function(x_values, y_values):
    """
    Creates an interpolation function that:
    1. Interpolates between given points
    2. Returns zero for values beyond the last data point
    3. Uses cubic spline interpolation for smooth curves
    """
    # Find the maximum x value where y is not zero
    # (Since these are cumulative distributions, we need to find where they reach their maximum)
    max_y = np.max(y_values)
    max_indices = np.where(y_values == max_y)[0]

    if len(max_indices) > 0:
        # The last point where the curve reaches its maximum
        last_valid_x = x_values[max_indices[-1]]
    else:
        # Default to the last x value if no maximum is found
        last_valid_x = x_values[-1]

    # Create cubic spline interpolation
    interp_func = interpolate.interp1d(
        x_values,
        y_values,
        kind="linear",
        bounds_error=False,
        fill_value=(0, max_y),  # Fill with 0 below range, max_y above range
    )

    # Return a wrapper function that implements our logic
    def query_function(x):
        """
        Query the interpolated value at x.
        Returns 0 if x is greater than the last valid data point.
        """
        # Convert input to numpy array if it's not already
        x_array = np.atleast_1d(x)
        result = np.zeros_like(x_array, dtype=float)

        # Calculate interpolated values only for points within valid range
        valid_indices = x_array <= last_valid_x
        if np.any(valid_indices):
            result[valid_indices] = interp_func(x_array[valid_indices])

        # If input was a scalar, return a scalar
        if np.isscalar(x):
            return float(result[0])
        return result

    return query_function


# Create interpolation functions for each class
interp_iec1 = create_interpolation_function(
    df_power_curves["speed_bin"], df_power_curves["iec_1"]
)
interp_iec2 = create_interpolation_function(
    df_power_curves["speed_bin"], df_power_curves["iec_2"]
)
interp_iec3 = create_interpolation_function(
    df_power_curves["speed_bin"], df_power_curves["iec_3"]
)
interp_offshore = create_interpolation_function(
    df_power_curves["speed_bin"], df_power_curves["offshore"]
)

# Access curves via dict
nrel_power_curves = {
    "iec1": interp_iec1,
    "iec2": interp_iec2,
    "iec3": interp_iec3,
    "offshore": interp_offshore,
}


#########################################
# Calculate bus-level power from genX
#########################################
def calculate_wind_timeseries_from_genX(
    df_genX,
    climate_paths,
    stab_coef_file,
    iec_curve,
    match_zones=True,
    wind_vars=["U10", "V10"],
    coef_cols=["month", "hour", "ZONE"],
    lat_name="south_north",
    lon_name="west_east",
    curvilinear=True,
    parallel=True,
):
    """
    Calculate wind power generation timeseries at bus level using genX outputs and climate data.

    This function combines wind resource data from climate model outputs with genX capacity
    information to calculate hourly wind power generation at each bus in the grid. It handles
    the conversion of wind speeds to power using NREL power curves and aggregates generation
    to the bus level.

    Parameters
    ----------
    df_genX : pd.DataFrame
        DataFrame containing genX outputs with columns ['latitude', 'longitude', 'EndCap', 'genX_zone']
    climate_paths : list
        List of paths to climate data files
    stab_coef_file : str
        Path to stability coefficients file
    iec_curve : str
        IEC wind turbine class to use ('iec1', 'iec2', 'iec3', or 'offshore')
    match_zones : bool, optional
        Whether to match zones when assigning to buses, by default True
    wind_vars : list, optional
        Wind variables to extract from climate data, by default ["U10", "V10"]
    coef_cols : list, optional
        Columns to use for stability coefficients, by default ["month", "hour", "ZONE"]
    lat_name : str, optional
        Name of latitude variable in climate data, by default "south_north"
    lon_name : str, optional
        Name of longitude variable in climate data, by default "west_east"
    curvilinear : bool, optional
        Whether climate data is on curvilinear grid, by default True
    parallel : bool, optional
        Whether to process data in parallel, by default True

    Returns
    -------
    pd.DataFrame
        DataFrame with hourly wind power generation at each bus, indexed by ['BUS_I', 'datetime']
        with column 'power_mw' containing the generation in megawatts
    """
    # Get raw wind data from climate outputs
    sites = np.column_stack((df_genX["latitude"], df_genX["longitude"]))

    df_wind = prepare_wind_data(
        climate_paths=climate_paths,
        wind_vars=wind_vars,
        sites=sites,
        stab_coef_file=stab_coef_file,
        coef_cols=coef_cols,
        lat_name=lat_name,
        lon_name=lon_name,
        curvilinear=curvilinear,
        parallel=parallel,
    )

    # Merge with genX outputs
    df = pd.merge(
        df_wind[["desired_lat", "desired_lon", "datetime", "ws_hubheight", "ZONE"]],
        df_genX[["latitude", "longitude", "EndCap", "genX_zone"]],
        how="outer",
        left_on=["desired_lat", "desired_lon"],
        right_on=["latitude", "longitude"],
    ).drop(columns=["desired_lat", "desired_lon"])

    # Calculate power
    df["power_MW"] = nrel_power_curves[iec_curve](df["ws_hubheight"]) * df["EndCap"]

    # Get unique genX locations (easier to assign to buses)
    gdf_genX_unique_locs = gpd.GeoDataFrame(
        df_genX[["latitude", "longitude", "genX_zone"]],
        geometry=gpd.points_from_xy(df_genX["longitude"], df_genX["latitude"]),
        crs="EPSG:4326",
    )

    # Assign to buses
    gdf_bus = gpd.read_file(f"{project_path}/data/grid/gis/Bus.shp")
    gdf_genX_unique_locs = nearest_neighbor_lat_lon(
        gdf_genX_unique_locs.rename(columns={"genX_zone": "ZONE"}),
        gdf_bus,
        match_zones=match_zones,
    )

    # Merge with timeseries and sum by bus
    df_out = (
        pd.merge(df, gdf_genX_unique_locs, on=["latitude", "longitude"], how="outer")
        .groupby(["BUS_I", "datetime"])[["power_MW"]]
        .sum()
    )

    # Return
    return df_out
