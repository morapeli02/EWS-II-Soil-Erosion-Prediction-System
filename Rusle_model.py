import ee
import geemap
from PIL import Image, ImageDraw, ImageFont
import os
import math
import pandas as pd
import requests  # ADD THIS
from io import BytesIO  # ADD THI

ee.Authenticate()
# Initialize Earth Engine
ee.Initialize(project="ee-makhosanemorapeli02")

# Define AOI

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################
# Define datasets
chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/PENTAD")
dem = ee.Image("USGS/SRTMGL1_003")
landcover = ee.ImageCollection("ESA/WorldCover/v100").first()
soil = ee.Image("OpenLandMap/SOL/SOL_TEXTURE-CLASS_USDA-TT_M/v02")
soilOrganic = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02")
s2_collection = ee.ImageCollection("COPERNICUS/S2_SR")  # Surface Reflectance product
s2_harmonized = ee.ImageCollection("COPERNICUS/S2_HARMONIZED")
modis_landcover = ee.ImageCollection("MODIS/006/MCD12Q1")  # Annual landcover (IGBP classification)

# Color palettes for factors
factor_palettes = {
    'R': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],  # Rainfall erosivity
    'K': ["#ffeda0", "#feb24c", "#fc4e2a", "#bd0026", "#800026"],  # Soil erodibility
    'LS': ["#edf8fb", "#b3cde3", "#8c96c6", "#8856a7", "#810f7c"],  # Slope length
    'C': ["#006837", "#31a354", "#78c679", "#c2e699", "#ffffcc"],   # Cover management
    'P': ["#08519c", "#3182bd", "#6baed6", "#bdd7e7", "#eff3ff"],   # Support practice
    'soil_loss': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]  # Final soil loss
}

# Define classification ranges and labels for each factor
factor_ranges = {
    'soil_loss': [0, 5, 10, 25, 50],  # t/ha/yr
    'soil_loss_labels': [
        "Low erosion (0-5 t/ha/yr)",
        "Slight erosion (5-10 t/ha/yr)",
        "Moderate erosion (10-25 t/ha/yr)",
        "High erosion (25-50 t/ha/yr)",
        "Severe erosion ( >50 t/ha/yr)"
    ]
}



###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def create_color_legend(colors, labels, filepath):
    """Enhanced legend creation with better formatting"""
    width, height = 400, len(colors) * 40
    legend = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(legend)
    font = ImageFont.load_default()
    
    for i, (color, label) in enumerate(zip(colors, labels)):
        y = i * 40
        # Larger color boxes
        draw.rectangle([10, y + 5, 50, y + 35], fill=color, outline="black")
        # More detailed labels with better positioning
        draw.text((60, y + 10), label, fill="black", font=font)
    
    legend.save(filepath)

def compute_r_factor(chirps, start_date, end_date, aoi):
    """
    Compute Rainfall Erosivity Factor (R) using southern Africa specific equation.
    
    R = 0.0132 * P^1.4100
    
    Where:
      P = annual rainfall in mm
    
    Reference: Le Roux et al. (2008) Water erosion prediction at a national scale for South Africa
    
    Args:
        chirps (ee.ImageCollection): CHIRPS pentad precipitation dataset (mm/pentad)
        start_date (str): Start date
        end_date (str): End date
        aoi (ee.Geometry): Area of Interest
    
    Returns:
        ee.Image: Rainfall Erosivity Factor
    """
    # Filter image collection for the year
    rainfall = chirps.filter(ee.Filter.date(start_date, end_date)).map(lambda img: img.clip(aoi))
    
    # Calculate annual rainfall (sum of all pentads)
    annual_rainfall = rainfall.sum().rename('R')
    
    # Compute R factor using southern African equation: R = 0.0132 * P^1.4100
    r_factor = annual_rainfall.expression(
        '0.0132 * pow(P, 1.41)',
        {'P': annual_rainfall}
    ).rename('R')
    
    return r_factor

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def interpolate_r_factor(r_factor, rain_gauge_points, aoi):
    """
    Perform Kriging interpolation of R factor using rain gauge stations
    
    Args:
        r_factor (ee.Image): Initial R factor image
        rain_gauge_points (ee.FeatureCollection): Rain gauge station locations with R factor values
        aoi (ee.Geometry): Area of interest
    
    Returns:
        ee.Image: Interpolated R factor image
    """
    # Perform Kriging interpolation
    interpolated_r_factor = (
        ee.Algorithms.If(
            rain_gauge_points.size().gt(0),
            ee.Algorithms.Terrain.Kriging(
                data=rain_gauge_points,
                property='R_value',
                region=aoi,
                scale=200,
                shape='circular'
            ),
            r_factor
        )
    )
    
    return ee.Image(interpolated_r_factor).clip(aoi).rename('R')

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def compute_k_factor(soil, soilOrganic, aoi, ndvi):
    """
    Corrected K factor using EPIC/Williams equation.
    Inputs as fractions (0-1), percentages in equation.
    Includes safeguards against negative exponents and extreme values.
    """
    print("Starting corrected K factor computation...")

    try:
        # Load and select surface (0 cm) - fractions 0-1
        sand_img = ee.Image("OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02").select('b0').divide(100.0).clip(aoi)
        clay_img = ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02").select('b0').divide(100.0).clip(aoi)
        
        silt_img = ee.Image(1.0).subtract(sand_img).subtract(clay_img).clip(aoi)  # silt fraction 0-1

        # Organic carbon: ×5 g/kg → divide by 50 → % OC
        org_c = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02").select('b0').divide(50.0).clip(aoi)

        # Safeguard: clamp fractions to valid 0-1, OC to realistic 0-10%
        sand_pct = sand_img.multiply(100).clamp(0, 100)
        silt_pct = silt_img.multiply(100).clamp(0, 100)
        clay_pct = clay_img.multiply(100).clamp(0, 100)
        org_c = org_c.clamp(0, 10)

    except Exception as e:
        print(f"Error loading soil assets: {e}. Using Lesotho fallback (loamy, ~1.5% OC).")
        sand_pct = ee.Image.constant(45).clip(aoi)
        silt_pct = ee.Image.constant(35).clip(aoi)
        clay_pct = ee.Image.constant(20).clip(aoi)
        org_c = ee.Image.constant(1.5).clip(aoi)

    # EPIC components - use safe expressions
    # f_csand = 0.2 + 0.3 * exp(-0.0256 * sand% * (1 - silt%/100))
    f_csand = ee.Image(0.2).add(
        ee.Image(0.3).multiply(
            ee.Image(-0.0256).multiply(sand_pct).multiply(
                ee.Image(1).subtract(silt_pct.divide(100))
            ).exp()
        )
    )

    # f_cl_si = (silt% / (clay% + silt%))^0.3
    f_cl_si = silt_pct.divide(clay_pct.add(silt_pct)).pow(0.3)

    # f_orgc = 1 - 0.25*OC / (OC + exp(3.72 - 2.95*OC))
    f_orgc = ee.Image(1).subtract(
        ee.Image(0.25).multiply(org_c).divide(
            org_c.add(ee.Image(3.72).subtract(ee.Image(2.95).multiply(org_c)).exp())
        )
    )

    # f_hisand = 1 - 0.7*(1-sand%/100) / [(1-sand%/100) + exp(-5.51 + 22.9*(1-sand%/100))]
    sand_frac = sand_pct.divide(100)
    f_hisand = ee.Image(1).subtract(
        ee.Image(0.7).multiply(ee.Image(1).subtract(sand_frac)).divide(
            ee.Image(1).subtract(sand_frac).add(
                ee.Image(-5.51).add(ee.Image(22.9).multiply(ee.Image(1).subtract(sand_frac))).exp()
            )
        )
    )

    # Combine and clamp to realistic range
    base_k = f_csand.multiply(f_cl_si).multiply(f_orgc).multiply(f_hisand)
    base_k = base_k.clamp(0.01, 0.7)  # Prevent extremes

    # Optional dynamic adjustment (small, safe)
    base_ndvi = ee.Number(0.3)
    soc_adjust = ndvi.subtract(base_ndvi).multiply(0.5).add(1).clamp(0.8, 1.2)  # Reduced multiplier
    k_factor = base_k.multiply(soc_adjust).rename('K').clip(aoi)

    # Metric units conversion (EPIC to RUSLE metric)
    k_factor = k_factor.multiply(0.1317)

    print("K factor computed (expected range 0.02-0.55).")
    return k_factor
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################
def get_annual_landcover(year, start_date, end_date, aoi):
    """Load annual landcover from MODIS with fallback for recent years"""
    # Try to get the specific year
    lc_collection = modis_landcover.filterDate(start_date, end_date)
    
    # Check if collection is empty
    count = lc_collection.size().getInfo()
    
    if count > 0:
        lc = lc_collection.first().select('LC_Type1').clip(aoi)
    else:
        # Fallback: Get the most recent available MODIS image
        print(f"MODIS landcover not yet available for {year}. Falling back to latest.")
        lc = modis_landcover.sort('system:time_start', False).first().select('LC_Type1').clip(aoi)
        
    return lc
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################


def compute_ls_factor(dem, aoi):
    """
    Compute LS factor optimized for Lesotho's mountainous terrain.
    
    Lesotho characteristics:
    - Steep slopes (often 15-45%)
    - Long hillslopes (100-500m common)
    - High elevation changes
    - Severe erosion risk
    
    Uses modified RUSLE equation with realistic bounds for mountain terrain.
    
    Reference: Renard et al. (1997) RUSLE + adaptations for steep terrain
    """
    # Calculate slope in degrees and radians
    slope_deg = ee.Terrain.slope(dem).clip(aoi)
    slope_rad = slope_deg.multiply(math.pi / 180)
    sin_slope = slope_rad.sin()
    
    # === SLOPE LENGTH ESTIMATION FOR MOUNTAINS ===
    # Option A: Use flow accumulation with mountain-appropriate caps
    merit = ee.Image("MERIT/Hydro/v1_0_1").clip(aoi)
    flow_acc_km2 = merit.select('upa')
    
    # Convert to flow path length (m)
    # Assume average hillslope width of 100m (reasonable for Lesotho valleys)
    # Length = Area / Width
    flow_length = flow_acc_km2.multiply(1000000).divide(100)  # km² to m²/100m = m
    
    # Cap to realistic hillslope lengths for Lesotho
    # Lower bound: 30m (minimum for erosion to develop)
    # Upper bound: 600m (beyond this, you're in channels/gullies)
    slope_length = flow_length.clamp(30, 600)
    
    # Resample to DEM resolution
    resolution = dem.projection().nominalScale()
    slope_length = slope_length.reproject(dem.projection())
    
    # === SLOPE LENGTH EXPONENT (m) ===
    # m varies with slope steepness
    # For steep slopes, m approaches 0.5; for gentle slopes, m approaches 0.3
    beta = sin_slope.divide(0.0896).divide(
        sin_slope.pow(0.8).multiply(3.0).add(0.56)
    )
    m = beta.divide(beta.add(1))
    
    # === L FACTOR (Length) ===
    L_factor = slope_length.divide(22.13).pow(m)
    
    # === S FACTOR (Steepness) - MODIFIED FOR STEEP SLOPES ===
    # Standard RUSLE S factor works up to ~50% slope
    # For Lesotho's very steep areas, use enhanced equations
    
    # For slopes < 9% (gentle)
    S_gentle = sin_slope.multiply(10.8).add(0.03)
    
    # For slopes 9-50% (moderate to steep)
    S_moderate = sin_slope.multiply(16.8).subtract(0.50)
    
    # For slopes > 50% (very steep - common in Lesotho highlands)
    # Use Liu et al. (2000) equation for steep slopes
    slope_pct = slope_deg.multiply(1.7453).tan().multiply(100)  # Convert to percent
    S_steep = sin_slope.multiply(21.91).subtract(0.96)
    
    # Combine using conditionals
    S_factor = ee.Image(0)
    S_factor = S_factor.where(slope_deg.lt(9), S_gentle)
    S_factor = S_factor.where(slope_deg.gte(9).And(slope_pct.lt(50)), S_moderate)
    S_factor = S_factor.where(slope_pct.gte(50), S_steep)
    
    # === COMBINE L AND S ===
    ls_factor = L_factor.multiply(S_factor)
    
    # === APPLY LESOTHO-SPECIFIC BOUNDS ===
    # Lesotho studies show LS can reach 50-100 in extreme areas
    # Cap at 100 to prevent unrealistic values while allowing high erosion zones
    ls_factor = ls_factor.clamp(0.1, 100)
    
    return ls_factor.rename('LS')

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################


def get_dynamic_landcover(year, aoi, start_date, end_date):
    """
    Get NDVI data from different satellites based on the year
    Years mapping:
    - 1995-2013: Landsat 5
    - 2014-2015: MODIS
    - 2015-present: Sentinel-2
    """
    try:
        if year >= 2015:
            # Use Sentinel-2 from 2015 onwards
            collection = ee.ImageCollection("COPERNICUS/S2_SR") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi) \
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
            def calculate_ndvi(image):
                ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
                return image.addBands(ndvi)
            ndvi_collection = collection.map(calculate_ndvi)
            annual_ndvi = ndvi_collection.select('NDVI').mean()
        elif year >= 2000:
            # Use MODIS from 2000 to 2015
            collection = ee.ImageCollection("MODIS/006/MOD13Q1") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi)
            annual_ndvi = collection.select('NDVI').mean().divide(10000.0)
        else:
            # Use Landsat 5 for 1995-2000
            collection = ee.ImageCollection("LANDSAT/LT05/C02/T1_L2") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi) \
                .filter(ee.Filter.lt('CLOUD_COVER', 20))
            def calculate_landsat_ndvi(image):
                nir = image.select('SR_B4').multiply(0.0000275).add(-0.2)
                red = image.select('SR_B3').multiply(0.0000275).add(-0.2)
                ndvi = nir.subtract(red).divide(nir.add(red)).rename('NDVI')
                return image.addBands(ndvi)
            ndvi_collection = collection.map(calculate_landsat_ndvi)
            annual_ndvi = ndvi_collection.select('NDVI').mean()
        
        # Clip and bound NDVI
        return annual_ndvi.clip(aoi).max(-1).min(1).rename('NDVI')
    
    except Exception as e:
        print(f"Error processing NDVI for year {year}: {str(e)}")
        return ee.Image.constant(0.3).clip(aoi).rename('NDVI')
    

###############################################################################################################################################################################################

def compute_c_factor_dynamic(year, aoi, start_date, end_date):
    """
    Compute C factor using NDVI with Van der Knijff et al. (2000) equation.
    
    C = exp( -α * (NDVI / (β - NDVI)) )
    
    α = 2, β = 1 (most widely used parameters)
    Reference: Van der Knijff et al. (2000)
    """
    ndvi = get_dynamic_landcover(year, aoi, start_date, end_date)
    
    # Constants as ee.Number
    alpha = ee.Number(2.0)
    beta = ee.Number(1.0)
    
    # Start EVERY operation from the IMAGE (ndvi) to avoid type issues
    # Step 1: β - NDVI   →   (beta - ndvi)
    beta_minus_ndvi = ndvi.multiply(-1).add(beta)   # Equivalent to beta - ndvi
    
    # Step 2: NDVI / (β - NDVI)
    fraction = ndvi.divide(beta_minus_ndvi)
    
    # Step 3: α * fraction
    scaled = fraction.multiply(alpha)
    
    # Step 4: - (α * fraction)
    negative_scaled = scaled.multiply(-1.0)
    
    # Step 5: exp(negative_scaled)
    c_factor = negative_scaled.exp()
    
    # Finalize
    c_factor = c_factor.rename('C').clip(aoi)
    
    # Safety bounds (C should be between 0 and 1)
    c_factor = c_factor.max(0).min(1)
    
    return c_factor
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def get_satellite_info(year):
    """
    Get information about which satellite was used for a specific year
    """
    if year >= 2015:
        return "Sentinel-2 (10m resolution)"
    elif year >= 2014:
        return "MODIS (250m resolution)"
    else:
        return "Landsat 5 (30m resolution)"
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def compute_p_factor(landcover, slope, aoi):
    """
    Compute P factor based on landcover and slope (improved from simple slope).
    
    Assign P values: e.g., forest=0.8-1, cropland=0.5-0.8 based on practices.
    For Lesotho, assume minimal practices, but use ESA classes.
    
    Reference: Common RUSLE adaptations.
    """
    """
    Updated: Use annual landcover to assign dynamic P values.
    Lower P for areas with conservation-implying cover (e.g., forest=0.9-1.0, cropland=0.4-0.6 if assumed practices).
    """
    # Base slope-based adjustment (existing)
    p_base = slope.expression(
        '(slope < 5) ? 0.6 : (slope < 10) ? 0.7 : (slope < 15) ? 0.8 : (slope < 20) ? 0.9 : 1.0',
        {'slope': slope}
    )
    
    # Dynamic landcover-based adjustment (IGBP examples; adjust per Lesotho context)
    p_adjust = landcover.expression(
        '(lc >= 1 && lc <= 5) ? 1.0 : ' +  # Forests: minimal erosion practices needed
        '(lc == 12 || lc == 14) ? 0.5 : ' +  # Croplands: assume some terracing/contours
        '(lc == 10) ? 0.8 : 1.0',  # Grasslands: moderate
        {'lc': landcover}
    )
    
    p_factor = p_base.multiply(p_adjust).clip(aoi).rename('P').max(0.1).min(1)
    return p_factor

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def compute_soil_loss(r, k, ls, c, p):
    return r.multiply(k).multiply(ls).multiply(c).multiply(p).rename('soil_loss')

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def get_soil_attributes(aoi):
    """
    Fetches Sand, Clay, and Organic Carbon from OpenLandMap.
    Calculates Silt as 100 - (Sand + Clay).
    """
    # 1. Load Sand Content (0cm depth)
    # Asset: OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02
    sand = ee.Image("OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02") \
        .select('b0') \
        .rename('sand') \
        .clip(aoi)

    # 2. Load Clay Content (0cm depth)
    # Asset: OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02
    clay = ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02") \
        .select('b0') \
        .rename('clay') \
        .clip(aoi)

    # 3. Calculate Silt Content
    # Silt = 100 - (Sand + Clay)
    silt = ee.Image(100).subtract(sand).subtract(clay).rename('silt')

    # 4. Load Organic Carbon (0cm depth)
    # Asset: OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02
    # Note: Values are in dg/kg (decigrams), so divide by 10 to get % or matches your K-factor formula needs
    org_c = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02") \
        .select('b0') \
        .rename('orgc') \
        .clip(aoi)

    # Combine all bands into one image
    soil_data = sand.addBands(clay).addBands(silt).addBands(org_c)
    
    return soil_data

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def process_year(year, output_dir, aoi, start_date, end_date):
    print(f"Processing year {year}...")
    
    r_factor = compute_r_factor(chirps, start_date, end_date, aoi)
    ndvi = get_dynamic_landcover(year, aoi, start_date, end_date)  # Reuse for C and now K proxy
    k_factor = compute_k_factor(soil, soilOrganic, aoi, ndvi)  # Now dynamic via NDVI
    ls_factor = compute_ls_factor(dem, aoi)
    c_factor = compute_c_factor_dynamic(year, aoi, start_date, end_date)
    slope = ee.Terrain.slope(dem)
    annual_lc = get_annual_landcover(year, start_date, end_date, aoi)
    p_factor = compute_p_factor(annual_lc, slope, aoi)  # Now dynamic
    soil_loss = compute_soil_loss(r_factor, k_factor, ls_factor, c_factor, p_factor)
    
    factors = {
        'R': r_factor,
        'K': k_factor,
        'LS': ls_factor,
        'C': c_factor,
        'P': p_factor,
        'soil_loss': soil_loss
    }
    
    for name, factor in factors.items():
        tif_path = os.path.join(output_dir, f"{year}_{name}.tif")
        # Visualization params (similar to original)
        stats = factor.reduceRegion(ee.Reducer.minMax(), aoi, 500).getInfo()
        min_val = stats.get(f'{name}_min', 0)
        max_val = stats.get(f'{name}_max', 100)
        vis = factor.visualize(min=min_val, max=max_val, palette=factor_palettes[name])
        geemap.ee_export_image(vis, filename=tif_path, scale=200, region=aoi)
        
        # Stats and legend (keep original functions)
        create_stats_and_legend(year, name, factor, output_dir, aoi)

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################



def create_stats_and_legend(year, name, factor, output_dir, aoi):
    """Create combined statistics and legend image with both existing and new enhanced statistics"""
    # Calculate comprehensive statistics
    stats = factor.reduceRegion(
        reducer=ee.Reducer.mean().combine(
            ee.Reducer.stdDev(), None, True
        ).combine(
            ee.Reducer.percentile([25, 50, 75]), None, True
        ).combine(
            ee.Reducer.sum(), None, True
        ).combine(
            ee.Reducer.max(), None, True
        ).combine(
            ee.Reducer.min(), None, True
        ),
        geometry=aoi,
        scale=200,
        maxPixels=1e9
    ).getInfo()
    
    if not stats:
        print(f"No data found for {name} factor in year {year}")
        return
    
    # FIX: Extract values using correct keys instead of positions
    mean_val = stats.get(f'{name}_mean', 0) or 0
    std_val = stats.get(f'{name}_stdDev', 0) or 0
    p25_val = stats.get(f'{name}_p25', 0) or 0
    median_val = stats.get(f'{name}_p50', 0) or 0
    p75_val = stats.get(f'{name}_p75', 0) or 0
    min_val = stats.get(f'{name}_min', 0) or 0
    max_val = stats.get(f'{name}_max', 0) or 0
    sum_val = stats.get(f'{name}_sum', 0) or 0
    
    # Calculate total area in hectares for soil_loss factor
    if name == 'soil_loss':
        total_area = aoi.area().divide(10000).getInfo()  # m² to hectares
        total_per_ha = sum_val / total_area if total_area > 0 else 0
    
    # Initialize drawing components
    font = ImageFont.load_default()
    
    # Calculate text heights and spacing
    padding = 20
    line_spacing = 25
    legend_item_height = 40
    legend_spacing = 45
    
    # Enhanced stats text with CORRECT values
    stats_text = [
        f"Statistical Summary:",
        f"Total {name}: {sum_val:.2f}",
        f"Mean: {mean_val:.2f}",
        f"Standard Deviation: {std_val:.2f}",
        f"25th Percentile: {p25_val:.2f}",
        f"Median: {median_val:.2f}",
        f"75th Percentile: {p75_val:.2f}",
        f"Minimum: {min_val:.2f}",
        f"Maximum: {max_val:.2f}"
    ]
    
    # Add total per hectare for soil_loss
    if name == 'soil_loss':
        stats_text.insert(2, f"Total per hectare: {total_per_ha:.2f} t/ha/yr")
    
    stats_height = len(stats_text) * line_spacing
    
    # Calculate legend section height
    if name == 'soil_loss':
        legend_items = factor_ranges['soil_loss_labels']
    else:
        legend_items = ["Very Low", "Low", "Moderate", "High", "Very High"]
    legend_height = len(legend_items) * legend_spacing
    
    # Calculate units section height
    units = {
        'R': "Rainfall erosivity factor (MJ mm ha⁻¹ h⁻¹ year⁻¹)",
        'K': "Soil erodibility factor (t ha h ha⁻¹ MJ⁻¹ mm⁻¹)",
        'LS': "Slope length and steepness factor (dimensionless)",
        'C': "Cover management factor (dimensionless)",
        'P': "Support practice factor (dimensionless)",
        'soil_loss': "Soil loss (t ha⁻¹ year⁻¹)"
    }
    units_height = 30 if name in units else 0
    
    if name == 'C':
        satellite_info = get_satellite_info(year)
        stats_text.append(f"\nData Source: {satellite_info}")
    
    # Calculate total image height with padding for additional content
    total_height = (
        padding +  # Top padding
        30 +      # Title height
        padding + # Space after title
        stats_height +
        padding + # Space before legend
        legend_height +
        padding + # Space before units
        units_height +
        padding   # Bottom padding
    )
    
    # Create image with calculated dimensions
    text_img = Image.new("RGB", (500, total_height), "white")
    draw = ImageDraw.Draw(text_img)
    
    # Draw title
    title = f"{name} Factor Analysis - Year {year}"
    current_y = padding
    draw.text((padding, current_y), title, fill="black", font=font)
    current_y += 30 + padding
    
    # Draw all statistics
    for text in stats_text:
        draw.text((padding, current_y), text, fill="black", font=font)
        current_y += line_spacing
    
    current_y += padding
    
    # Draw legend (existing code)
    if name == 'soil_loss':
        for i, (color, label) in enumerate(zip(factor_palettes[name], factor_ranges['soil_loss_labels'])):
            draw.rectangle(
                [(padding, current_y), 
                 (padding + 40, current_y + legend_item_height)], 
                fill=color, 
                outline="black"
            )
            draw.text(
                (padding + 60, current_y + 5),
                label,
                fill="black",
                font=font
            )
            current_y += legend_spacing
    else:
        labels = ["Very Low", "Low", "Moderate", "High", "Very High"]
        for i, (color, label) in enumerate(zip(factor_palettes[name], labels)):
            draw.rectangle(
                [(padding, current_y), 
                (padding + 40, current_y + legend_item_height)], 
                fill=color, 
                outline="black"
            )
            draw.text(
                (padding + 60, current_y + 5),
                label,
                fill="black",
                font=font
            )
            current_y += legend_spacing
    
    # Draw units if available
    if name in units:
        current_y += padding
        draw.text(
            (padding, current_y),
            units[name],
            fill="black",
            font=font
        )
    
    # Save the image
    output_path = os.path.join(output_dir, f"{year}_{name}_stats.png")
    text_img.save(output_path)

    # If it's soil loss, create enhanced detailed analysis
    if name == 'soil_loss':
        create_detailed_soil_loss_analysis(year, factor, stats, output_dir, aoi)

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################



def create_detailed_soil_loss_analysis(year, factor, stats, output_dir, aoi):
    """Create a detailed analysis image with enhanced erosion calculations while preserving existing analysis"""
    
    # FIX: Extract using correct keys instead of assuming positions
    mean_val = stats.get('soil_loss_mean', 0) or 0
    median_val = stats.get('soil_loss_p50', 0) or 0
    min_val = stats.get('soil_loss_min', 0) or 0
    max_val = stats.get('soil_loss_max', 0) or 0
    std_val = stats.get('soil_loss_stdDev', 0) or 0
    sum_val = stats.get('soil_loss_sum', 0) or 0
    
    # Calculate areas for each erosion class, including values above 50
    areas = {}
    
    # Calculate area for each range
    ranges = list(zip(factor_ranges['soil_loss'][:-1], factor_ranges['soil_loss'][1:]))
    for i, (min_range, max_range) in enumerate(ranges):
        area = factor.gte(min_range).And(factor.lt(max_range))\
            .multiply(ee.Image.pixelArea())\
            .divide(10000)\
            .reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=aoi,
                scale=200,
                maxPixels=1e9
            ).getInfo()
        
        label = factor_ranges['soil_loss_labels'][i]
        areas[label] = area.get('soil_loss', 0) or 0
    
    # Calculate area for values >= 50 (severe erosion)
    severe_area = factor.gte(50)\
        .multiply(ee.Image.pixelArea())\
        .divide(10000)\
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=200,
            maxPixels=1e9
        ).getInfo()
    
    areas['Severe erosion ( >50 t/ha/yr)'] = severe_area.get('soil_loss', 0) or 0
    
    # Calculate total erosion and total area
    total_erosion = factor.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=200,
        maxPixels=1e9
    ).getInfo().get('soil_loss', 0) or 0
    
    # Calculate total area in hectares
    total_area_ha = aoi.area().divide(10000).getInfo()
    
    # Calculate total soil loss per hectare
    total_per_ha = total_erosion / total_area_ha if total_area_ha > 0 else 0
    
    # Create detailed analysis image with increased height for additional content
    img = Image.new("RGB", (600, 750), "white")  # Increased height for new stat
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    
    # Draw title
    draw.text((10, 10), f"Detailed Soil Loss Analysis - Year {year}", fill="black", font=font)
    
    # Draw existing area analysis
    draw.text((10, 50), "Area Under Each Erosion Class (hectares):", fill="black", font=font)
    y_pos = 80
    for label, area in areas.items():
        draw.text((10, y_pos), f"{label}: {area:.2f} ha", fill="black", font=font)
        y_pos += 25
    
    # Draw enhanced analysis section
    draw.text((10, y_pos + 20), "Enhanced Analysis:", fill="black", font=font)
    y_pos += 50
    
    # Calculate comprehensive statistics
    total_area = sum(areas.values())
    severe_area_val = areas['Severe erosion ( >50 t/ha/yr)']
    severe_percent = (severe_area_val / total_area) * 100 if total_area > 0 else 0
    
    # Calculate average soil loss rate
    avg_rate = (total_erosion / total_area) if total_area > 0 else 0
    
    # Combine existing and new findings with FIXED values
    findings = [
        # Existing metrics
        f"Total analyzed area: {total_area:.2f} ha",
        f"Area under severe erosion: {severe_area_val:.2f} ha ({severe_percent:.1f}%)",
        f"Mean soil loss: {mean_val:.2f} t/ha/year",
        f"Median soil loss: {median_val:.2f} t/ha/year",
        f"Standard deviation: {std_val:.2f} t/ha/year",
        # New enhanced metrics
        f"Total soil loss: {total_erosion:.2f} tonnes/year",
        f"Total soil loss per hectare: {total_per_ha:.2f} t/ha/year",  # NEW!
        f"Average soil loss rate: {avg_rate:.2f} t/ha/year",
        f"Maximum recorded soil loss: {max_val:.2f} t/ha/year",
        f"Minimum recorded soil loss: {min_val:.2f} t/ha/year",
        f"Percentage of land under severe erosion: {severe_percent:.1f}%"
    ]
    
    for finding in findings:
        draw.text((10, y_pos), finding, fill="black", font=font)
        y_pos += 25
    
    # Save the enhanced analysis
    output_path = os.path.join(output_dir, f"{year}_soil_loss_detailed_analysis.png")
    img.save(output_path)
    print(f"Detailed soil loss analysis saved to {output_path}")
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def download_aoi_visualizations_gee(region_name, aoi, output_dir):
    """
    Download HIGH-QUALITY satellite visualizations with custom labels (schools, hospitals, etc.)
    Combines Esri base maps with GEE data for detailed annotations
    
    Args:
        region_name: Name of the region
        aoi: Earth Engine Geometry (created by create_square_aoi)
        output_dir: Output directory for this region
    """
    import requests
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    
    viz_dir = os.path.join(output_dir, "aoi_visualization")
    os.makedirs(viz_dir, exist_ok=True)
    
    print(f"\nDownloading HIGH-QUALITY satellite maps for {region_name}...")
    
    # Extract EXACT coordinates from the AOI polygon (not bounds)
    try:
        # Get the actual polygon coordinates
        coords_info = aoi.coordinates().getInfo()
        
        # Extract the coordinate ring (first element for simple polygon)
        coord_ring = coords_info[0]
        
        # Get all longitudes and latitudes
        lons = [coord[0] for coord in coord_ring]
        lats = [coord[1] for coord in coord_ring]
        
        # Get the exact bounds from the actual coordinates
        min_lon = min(lons)
        max_lon = max(lons)
        min_lat = min(lats)
        max_lat = max(lats)
        
        center_lon = (min_lon + max_lon) / 2
        center_lat = (min_lat + max_lat) / 2
        
        # Format bbox with consistent precision (6 decimal places to match your AOI)
        bbox = f"{min_lon:.15f},{min_lat:.15f},{max_lon:.15f},{max_lat:.15f}"
        
        print(f"  AOI bounds: [{min_lat:.15f}, {min_lon:.15f}] to [{max_lat:.15f}, {max_lon:.15f}]")
        print(f"  Center: [{center_lat:.15f}, {center_lon:.15f}]")
        print(f"  Size: {abs(max_lon - min_lon):.15f}° x {abs(max_lat - min_lat):.15f}°")
        
    except Exception as e:
        print(f"  ✗ Error getting AOI coordinates: {str(e)}")
        return
    
    # High-quality parameters
    HIGH_RES_SIZE = '2400,2400'
    DPI = 300
    
    # Base parameters for all requests
    base_params = {
        'bbox': bbox,
        'bboxSR': 4326,
        'size': HIGH_RES_SIZE,
        'imageSR': 4326,
        'format': 'png32',
        'dpi': DPI,
        'f': 'image'
    }
    
    # === 1. Base: High-res satellite imagery ===
    try:
        print("  Downloading HIGH-RES satellite imagery...")
        
        esri_url = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
        params = base_params.copy()
        params['transparent'] = False
        
        response = requests.get(esri_url, params=params, timeout=120)
        response.raise_for_status()
        
        base_img = Image.open(BytesIO(response.content))
        output_path = os.path.join(viz_dir, f"{region_name}_satellite.png")
        base_img.save(output_path, quality=100, dpi=(DPI, DPI), optimize=False)
        print(f"  ✓ Satellite imagery saved: {output_path}")
        
    except Exception as e:
        print(f"  ✗ Error downloading satellite: {str(e)}")
        return
    
    # === 2. Enhanced Hybrid with Custom Labels ===
    try:
        print("  Creating enhanced map with custom labels (schools, hospitals, etc.)...")
        
        # Download base satellite again for the labeled version
        params = base_params.copy()
        params['transparent'] = False
        
        response_sat = requests.get(esri_url, params=params, timeout=120)
        
        if not response_sat.ok:
            print(f"  ✗ Failed to download base satellite: {response_sat.status_code}")
            print(f"  Response: {response_sat.text[:200]}")
            return
        
        sat_img = Image.open(BytesIO(response_sat.content)).convert('RGBA')
        img_width, img_height = sat_img.size
        print(f"    Base image size: {img_width}x{img_height}")
        
        # Add Esri overlay layers
        print("    Adding Esri reference layers...")
        
        # Layer 1: Transportation
        try:
            transport_params = base_params.copy()
            transport_params['transparent'] = True
            
            response_transport = requests.get(
                "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/export",
                params=transport_params,
                timeout=120
            )
            if response_transport.ok:
                transport_img = Image.open(BytesIO(response_transport.content)).convert('RGBA')
                sat_img = Image.alpha_composite(sat_img, transport_img)
                print("    ✓ Added roads and transportation")
            else:
                print(f"    ⚠ Transportation layer failed: {response_transport.status_code}")
        except Exception as e:
            print(f"    ⚠ Transportation layer error: {str(e)}")
        
        # Layer 2: Boundaries and Places
        try:
            boundaries_params = base_params.copy()
            boundaries_params['transparent'] = True
            
            response_boundaries = requests.get(
                "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/export",
                params=boundaries_params,
                timeout=120
            )
            if response_boundaries.ok:
                boundaries_img = Image.open(BytesIO(response_boundaries.content)).convert('RGBA')
                sat_img = Image.alpha_composite(sat_img, boundaries_img)
                print("    ✓ Added boundaries and place names")
            else:
                print(f"    ⚠ Boundaries layer failed: {response_boundaries.status_code}")
        except Exception as e:
            print(f"    ⚠ Boundaries layer error: {str(e)}")
        
        # Layer 3: Reference overlay (rivers, water)
        try:
            ref_params = base_params.copy()
            ref_params['transparent'] = True
            
            response_ref = requests.get(
                "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Reference_Overlay/MapServer/export",
                params=ref_params,
                timeout=120
            )
            if response_ref.ok:
                ref_img = Image.open(BytesIO(response_ref.content)).convert('RGBA')
                sat_img = Image.alpha_composite(sat_img, ref_img)
                print("    ✓ Added rivers and water bodies")
            else:
                print(f"    ⚠ Reference overlay failed: {response_ref.status_code}")
        except Exception as e:
            print(f"    ⚠ Reference overlay error: {str(e)}")
        
        # === Add Custom POI Labels ===
        print("    Adding custom POI markers...")
        
        # Create a drawable overlay
        overlay = Image.new('RGBA', sat_img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Load fonts
        try:
            font_size = 24
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
                font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
            except:
                try:
                    font = ImageFont.truetype("arial.ttf", font_size)
                    font_small = ImageFont.truetype("arial.ttf", 18)
                except:
                    font = ImageFont.load_default()
                    font_small = ImageFont.load_default()
        except:
            font = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # Helper function to convert lat/lon to pixel coordinates
        def latlon_to_pixel(lat, lon):
            x = int((lon - min_lon) / (max_lon - min_lon) * img_width)
            y = int((max_lat - lat) / (max_lat - min_lat) * img_height)
            return x, y
        
        # Query OpenStreetMap for POIs
        try:
            print("    Querying OpenStreetMap for points of interest...")
            
            overpass_url = "http://overpass-api.de/api/interpreter"
            
            overpass_query = f"""
            [out:json][timeout:60];
            (
              node["amenity"="school"]({min_lat},{min_lon},{max_lat},{max_lon});
              node["amenity"="hospital"]({min_lat},{min_lon},{max_lat},{max_lon});
              node["amenity"="clinic"]({min_lat},{min_lon},{max_lat},{max_lon});
              node["amenity"="place_of_worship"]({min_lat},{min_lon},{max_lat},{max_lon});
              node["amenity"="marketplace"]({min_lat},{min_lon},{max_lat},{max_lon});
              node["amenity"="community_centre"]({min_lat},{min_lon},{max_lat},{max_lon});
            );
            out body;
            """
            
            response = requests.post(overpass_url, data={'data': overpass_query}, timeout=60)
            
            if response.ok:
                data = response.json()
                elements = data.get('elements', [])
                
                print(f"    Found {len(elements)} points of interest")
                
                # Draw each POI
                for element in elements:
                    if 'lat' in element and 'lon' in element:
                        lat = element['lat']
                        lon = element['lon']
                        tags = element.get('tags', {})
                        amenity = tags.get('amenity', 'unknown')
                        name = tags.get('name', amenity.capitalize())
                        
                        x, y = latlon_to_pixel(lat, lon)
                        
                        # Choose color based on type
                        if amenity == 'school':
                            color = (0, 0, 255, 255)  # Blue
                        elif amenity in ['hospital', 'clinic']:
                            color = (255, 0, 0, 255)  # Red
                        elif amenity == 'place_of_worship':
                            color = (128, 0, 128, 255)  # Purple
                        elif amenity == 'marketplace':
                            color = (255, 165, 0, 255)  # Orange
                        elif amenity == 'community_centre':
                            color = (0, 128, 0, 255)  # Green
                        else:
                            color = (128, 128, 128, 255)  # Gray
                        
                        # Draw circle marker
                        radius = 12
                        draw.ellipse([x-radius, y-radius, x+radius, y+radius], 
                                   fill=color, outline=(255, 255, 255, 255), width=3)
                        
                        # Draw label with background
                        label = name[:30]
                        text_width = len(label) * 10
                        text_height = 20
                        
                        text_x = x + 15
                        text_y = y - 10
                        draw.rectangle([text_x-2, text_y-2, text_x+text_width+2, text_y+text_height+2],
                                     fill=(255, 255, 255, 200))
                        draw.text((text_x, text_y), label, fill=(0, 0, 0, 255), font=font_small)
                
                print(f"    ✓ Labeled {len(elements)} locations")
            else:
                print(f"    ⚠ Overpass API request failed: {response.status_code}")
        
        except Exception as e:
            print(f"    ⚠ Error querying OpenStreetMap: {str(e)}")
        
        # Composite the overlay onto the satellite image
        sat_img = Image.alpha_composite(sat_img, overlay)
        
        # Save the final annotated image
        output_path = os.path.join(viz_dir, f"{region_name}_satellite_labeled.png")
        sat_img.save(output_path, quality=100, dpi=(DPI, DPI), optimize=False)
        print(f"  ✓ Labeled satellite map saved: {output_path}")
        
    except Exception as e:
        print(f"  ✗ Error creating labeled map: {str(e)}")
        import traceback
        traceback.print_exc()
    
    # === 3. Create legend image ===
    try:
        print("  Creating legend...")
        
        legend_img = Image.new('RGBA', (400, 300), (255, 255, 255, 255))
        legend_draw = ImageDraw.Draw(legend_img)
        
        try:
            legend_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except:
            legend_font = ImageFont.load_default()
        
        legend_draw.text((10, 10), "Map Legend:", fill=(0, 0, 0, 255), font=legend_font)
        
        legend_items = [
            (30, "Schools", (0, 0, 255, 255)),
            (60, "Hospitals/Clinics", (255, 0, 0, 255)),
            (90, "Places of Worship", (128, 0, 128, 255)),
            (120, "Markets", (255, 165, 0, 255)),
            (150, "Community Centers", (0, 128, 0, 255)),
        ]
        
        for y_pos, label, color in legend_items:
            legend_draw.ellipse([10, y_pos, 22, y_pos+12], fill=color, outline=(255, 255, 255, 255), width=2)
            legend_draw.text((30, y_pos), label, fill=(0, 0, 0, 255), font=legend_font)
        
        legend_path = os.path.join(viz_dir, f"{region_name}_legend.png")
        legend_img.save(legend_path, quality=100)
        print(f"  ✓ Legend saved: {legend_path}")
        
    except Exception as e:
        print(f"  ✗ Error creating legend: {str(e)}")
    
    print(f"✓ All maps with custom labels downloaded for {region_name}\n")
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

def main():
    # Create a regions dataframe or load from CSV
    regions_data = [
        #{"region_name": "Tosing", "center_lat": -30.342043706671383, "center_lon":  27.929300128161906, "distance_km": 10},
        # {"region_name": "Linakeng", "center_lat": -29.52198184488981,  "center_lon": 28.867585216127754, "distance_km": 10},
        # {"region_name": "Qibing", "center_lat": -29.692672088712456,  "center_lon": 27.101262321394724, "distance_km": 10},
        # {"region_name": "Tsoelike", "center_lat": -30.017465520190267,  "center_lon": 28.66880599364324, "distance_km": 10},
        # {"region_name": "Sanqebethu", "center_lat": -29.341522496809645,  "center_lon": 29.176083045693126, "distance_km": 10},
        # {"region_name": "Mphosong", "center_lat": -29.0255874847322, "center_lon": 28.293781911919545, "distance_km": 10},
        #{"region_name": "Lesotho", "center_lat": -29.611587643481293,   "center_lon": 28.41379432778028, "distance_km": 270}
        # Add more regions as needed
        {"region_name": "Koalabata", "center_lat": -29.293572068575394,   "center_lon": 27.558810363666947, "distance_km": 5}
    ]
    
    # Convert list to DataFrame for easier management
    regions_df = pd.DataFrame(regions_data)
    
    # Save regions to CSV for future use
    regions_df.to_csv("regions.csv", index=False)
    
    # Alternatively, load regions from existing CSV
    # regions_df = pd.read_csv("regions.csv")
    
    # Process each region for each year
    for index, region in regions_df.iterrows():
        region_name = region["region_name"]
        center_lat = float(region["center_lat"])
        center_lon = float(region["center_lon"])
        distance = float(region["distance_km"])
        
        # Create AOI geometry for this region
        aoi = create_square_aoi(center_lat, center_lon, distance)
        
        # Create region-specific output directory
        region_output_dir = f"./RUSLE_Outputs/{region_name}"
        os.makedirs(region_output_dir, exist_ok=True)
        # Download and save AOI visualization images using GEE
        download_aoi_visualizations_gee(region_name, aoi, region_output_dir)
        print(f"Processing region: {region_name}")
        
        # Process each year for this region
        for year in range(2024, 2025):
            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"
            
            # Create year-specific output directory
            year_output_dir = f"{region_output_dir}/{year}"
            os.makedirs(year_output_dir, exist_ok=True)
            
            #process_year(year, year_output_dir, aoi, start_date, end_date)
            print(f"  Completed processing for year {year}")

def create_square_aoi(center_lat, center_lon, distance):
    """Create a square AOI centered at the given coordinates with the specified distance (km)"""
    # Half the side length
    distance = distance / 2
    
    # Convert distance from km to degrees
    lat_offset = distance / 111.32  # 1 degree latitude ≈ 111.32 km
    lon_offset = distance / (111.32 * math.cos(math.radians(center_lat)))  # Adjust for longitude

    # Create a square polygon centered at the given coordinates
    coordinates = [
        [[center_lon - lon_offset, center_lat - lat_offset],
         [center_lon - lon_offset, center_lat + lat_offset],
         [center_lon + lon_offset, center_lat + lat_offset],
         [center_lon + lon_offset, center_lat - lat_offset],
         [center_lon - lon_offset, center_lat - lat_offset]]
    ]
    
    return ee.Geometry.Polygon(coordinates)

###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################


if __name__ == "__main__":
    main()
    
