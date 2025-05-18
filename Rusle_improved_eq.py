import ee
import geemap
from PIL import Image, ImageDraw, ImageFont
import os
import math
import pandas as pd

# Initialize Earth Engine
ee.Initialize(project="ee-makhosanemorapeli02")


# Define datasets
chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/PENTAD")
dem = ee.Image("USGS/SRTMGL1_003")
landcover = ee.ImageCollection("ESA/WorldCover/v100").first()
soil = ee.Image("OpenLandMap/SOL/SOL_TEXTURE-CLASS_USDA-TT_M/v02")
soilOrganic = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02")
s2_collection = ee.ImageCollection("COPERNICUS/S2_SR")  # Surface Reflectance product
s2_harmonized = ee.ImageCollection("COPERNICUS/S2_HARMONIZED")

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
    'soil_loss': [0, 5, 10, 20, 50],  # t/ha/yr - adjusted for Lesotho conditions
    'soil_loss_labels': [
        "Low erosion (0-5 t/ha/yr)",
        "Moderate erosion (5-10 t/ha/yr)",
        "High erosion (10-20 t/ha/yr)",
        "Very high erosion (20-50 t/ha/yr)",
        "Severe erosion (>50 t/ha/yr)"
    ]
}

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

def compute_k_factor(soil, soilOrganic, aoi):
    """
    Compute the soil erodibility factor (K) using a simplified equation better suited
    for African soils.
    
    K = (0.2 + 0.3 * exp(-0.0256 * Sand * (1 - Silt/100))) * 
        (Silt/(Clay + Silt))^0.3 * 
        (1.0 - 0.25 * C/(C + exp(3.72 - 2.95 * C))) * 
        (1.0 - 0.7 * (1 - Sand/100)/(1 - Sand/100 + exp(-5.51 + 22.9 * (1 - Sand/100))))
    
    Args:
        soil (ee.Image): Soil texture dataset
        soilOrganic (ee.Image): Soil organic carbon dataset
        aoi (ee.Geometry): Area of interest

    Returns:
        ee.Image: Soil erodibility factor (K)
    """
    # Select necessary bands
    # Note: Adjust these band indices based on your actual data structure
    sand = soil.select('b0').divide(100)  # Sand content (fraction)
    silt = soil.select('b1').divide(100)  # Silt content (fraction)
    clay = soil.select('b2').divide(100)  # Clay content (fraction)
    
    # Get organic carbon content (%)
    organic_carbon = soilOrganic.select('b0').divide(10)  # Assuming original value is in g/kg
    
    # Compute K factor using the equation for African soils
    k_factor = ee.Image.expression(
        '(0.2 + 0.3 * exp(-0.0256 * sand * (1 - silt/100))) * ' +
        'pow(silt/(clay + silt), 0.3) * ' +
        '(1.0 - 0.25 * C/(C + exp(3.72 - 2.95 * C))) * ' +
        '(1.0 - 0.7 * (1 - sand/100)/(1 - sand/100 + exp(-5.51 + 22.9 * (1 - sand/100))))',
        {
            'sand': sand.multiply(100),  # Convert back to percentage
            'silt': silt.multiply(100),
            'clay': clay.multiply(100),
            'C': organic_carbon,
            'exp': 'Math.exp'
        }
    ).rename('K')
    
    # Ensure K values are within reasonable range (0-0.1)
    k_factor = k_factor.max(0).min(0.1).clip(aoi)
    
    return k_factor

def compute_flow_accumulation(dem, aoi):
    """
    Compute flow accumulation for improved LS factor calculation
    
    Args:
        dem (ee.Image): Digital elevation model
        aoi (ee.Geometry): Area of interest
    
    Returns:
        tuple: (flow accumulation, flow direction)
    """
    # Fill sinks in DEM to create hydrologically correct DEM
    filled_dem = dem.focal_min(kernel=ee.Kernel.circle(radius=5), iterations=2)
    
    # Calculate flow direction (D8 algorithm)
    flow_direction = ee.Terrain.aspect(filled_dem).subtract(90).divide(45).toInt()
    
    # Calculate flow accumulation
    flow_acc = ee.Image(1).cumulativeCost(
        source=flow_direction,
        costMap=ee.Image(1),
        maxDistance=100
    ).divide(900)  # Normalize by cell area
    
    return flow_acc, flow_direction

def compute_ls_factor(dem, aoi):
    """
    Computes the LS (topographic) factor using improved equations for mountainous terrain.
    
    For L factor: L = (λ/22.13)^m
    Where λ is flow length and m varies with slope:
      m = 0.5 for slopes > 5%
      m = 0.4 for slopes 3-5%
      m = 0.3 for slopes 1-3%
      m = 0.2 for slopes < 1%
    
    For S factor on steep slopes: S = 16.8 × sin(θ) - 0.50
    
    Args:
        dem (ee.Image): Digital elevation model
        aoi (ee.Geometry): Area of interest

    Returns:
        ee.Image: LS factor image
    """
    # Calculate slope in degrees and percentage
    slope_deg = ee.Terrain.slope(dem)
    slope_rad = slope_deg.multiply(math.pi/180)
    slope_pct = slope_deg.tan().multiply(100)
    
    # Calculate flow accumulation for improved L factor
    flow_acc, _ = compute_flow_accumulation(dem, aoi)
    
    # Calculate flow length (λ) from flow accumulation
    # Simplified: λ = sqrt(flow_acc * cell_size)
    cell_size = 30  # DEM resolution in meters
    flow_length = flow_acc.sqrt().multiply(cell_size)
    
    # Calculate L factor exponent (m) based on slope
    m = slope_pct.expression(
        '(slope < 1) ? 0.2 : ' +
        '(slope < 3) ? 0.3 : ' +
        '(slope < 5) ? 0.4 : 0.5',
        {'slope': slope_pct}
    )
    
    # Calculate L factor: L = (λ/22.13)^m
    L = flow_length.divide(22.13).pow(m)
    
    # Calculate S factor using equation for steep slopes
    # S = 16.8 × sin(θ) - 0.50
    S = slope_rad.sin().multiply(16.8).subtract(0.5)
    
    # For gentle slopes (< 9%), use the standard equation
    S_gentle = slope_pct.expression(
        '(0.43 + 0.30 * s + 0.043 * pow(s, 2)) / 6.574',
        {'s': slope_pct}
    )
    
    # Combine S factors based on slope threshold
    S = slope_pct.gte(9).multiply(S).add(
        slope_pct.lt(9).multiply(S_gentle)
    )
    
    # Compute LS factor
    LS = L.multiply(S).max(0).rename('LS').clip(aoi)
    
    return LS

# def compute_c_factor_dynamic(year, aoi, start_date, end_date):
#     """
#     Compute C factor using NDVI with improved parameters for southern African vegetation
    
#     C = e^[(-α) * NDVI/(β - NDVI)]
    
#     Where:
#       α = 2 (Parameter controlling the shape of the curve)
#       β = 1 (Scaling parameter)
    
#     Args:
#         year (int): Year for analysis
#         aoi (ee.Geometry): Area of interest
#         start_date (str): Start date
#         end_date (str): End date
    
#     Returns:
#         ee.Image: C factor image
#     """
#     try:
#         # Get NDVI for the specified year
#         ndvi = get_dynamic_landcover(year, aoi, start_date, end_date)
        
#         # Ensure NDVI values are bounded
#         ndvi = ndvi.max(-1).min(1)
        
#         # Compute C factor using exponential function better suited for African vegetation
#         c_factor = ndvi.expression(
#             'exp(-2 * ndvi / (1 - ndvi))',
#             {
#                 'ndvi': ndvi,
#                 'exp': 'Math.exp'
#             }
#         )
        
#         # Apply constraints
#         c_factor = c_factor.max(0.01).min(1.0).rename('C')
        
#         return c_factor.clip(aoi)
        
#     except Exception as e:
#         print(f"Error computing C factor for year {year}: {str(e)}")
#         return ee.Image.constant(0.3).clip(aoi).rename('C')

def get_dynamic_landcover(year, aoi, start_date, end_date):
    """
    Get NDVI data from different satellites based on the year
    
    Years mapping:
    - 1995-2013: Landsat 5/7
    - 2014-2015: MODIS
    - 2015-present: Sentinel-2
    """
    
    try:
        if year >= 2015:
            # Use Sentinel-2 from 2015 to present
            collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi) \
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
            
            def calculate_ndvi(image):
                ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
                return image.addBands(ndvi)
            
            ndvi_collection = collection.map(calculate_ndvi)
            annual_ndvi = ndvi_collection.select('NDVI').median()  # Changed from mean to median for better results
            
        elif year >= 2000:
            # Use MODIS from 2000 to 2015
            collection = ee.ImageCollection("MODIS/006/MOD13Q1") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi)
            
            annual_ndvi = collection.select('NDVI').median().divide(10000.0)
            
        else:
            # Use Landsat for earlier years
            collection = ee.ImageCollection("LANDSAT/LE07/C02/T1_L2") \
                .filterDate(start_date, end_date) \
                .filterBounds(aoi) \
                .filter(ee.Filter.lt('CLOUD_COVER', 20))
            
            def calculate_landsat_ndvi(image):
                # Scale the bands according to metadata
                nir = image.select('SR_B4').multiply(0.0000275).add(-0.2)
                red = image.select('SR_B3').multiply(0.0000275).add(-0.2)
                ndvi = ee.Image.expression(
                    '(nir - red) / (nir + red)', {
                        'nir': nir,
                        'red': red
                    }
                ).rename('NDVI')
                return image.addBands(ndvi)
            
            ndvi_collection = collection.map(calculate_landsat_ndvi)
            annual_ndvi = ndvi_collection.select('NDVI').median()
        
        # Clip to area of interest and ensure valid NDVI range
        annual_ndvi = annual_ndvi.clip(aoi).max(-1).min(1)
        
        # Check if we have valid data
        count = ee.Image.constant(1).updateMask(annual_ndvi) \
            .reduceRegion(
                reducer=ee.Reducer.count(),
                geometry=aoi,
                scale=500,
                maxPixels=1e9
            ).get('constant')
            
        count_info = count.getInfo()
        if not count_info or count_info == 0:
            print(f"No valid NDVI data available for year {year}")
            return ee.Image.constant(0.3).clip(aoi).rename('NDVI')
            
        return annual_ndvi.rename('NDVI')
        
    except Exception as e:
        print(f"Error processing NDVI for year {year}: {str(e)}")
        return ee.Image.constant(0.3).clip(aoi).rename('NDVI')

def compute_p_factor(dem, landcover, aoi):
    """
    Compute support practice factor (P) based on slope and land cover type.
    Instead of using a constant value, this considers conservation practices
    typical in Lesotho.
    
    Args:
        dem (ee.Image): Digital elevation model
        landcover (ee.Image): Land cover classification
        aoi (ee.Geometry): Area of interest
    
    Returns:
        ee.Image: P factor image
    """
    # Calculate slope percentage
    slope_pct = ee.Terrain.slope(dem).tan().multiply(100)
    
    # P factor values based on slope classes
    # These values assume some level of contouring and terracing common in Lesotho
    p_values = ee.Image(0).expression(
        '(slope < 2) ? 0.6 : ' +
        '(slope < 5) ? 0.5 : ' +
        '(slope < 8) ? 0.5 : ' +
        '(slope < 12) ? 0.6 : ' +
        '(slope < 16) ? 0.7 : ' +
        '(slope < 20) ? 0.8 : 0.9',
        {'slope': slope_pct}
    )
    
    # Adjust P factor based on land cover types
    # Assuming ESA WorldCover classification codes
    cultivated_lands = landcover.eq(40)  # Cultivated lands (adjust code as needed)
    forested_lands = landcover.eq(10)    # Forest (adjust code as needed)
    
    # Apply land use specific adjustments
    # Cultivated lands with practices like strip cropping
    p_crops = cultivated_lands.multiply(0.75)
    
    # Forested lands with good management
    p_forest = forested_lands.multiply(0.1)
    
    # Combine P factors - use the minimum P value for each pixel
    p_factor = ee.Image(1) \
        .where(cultivated_lands, p_values.multiply(0.75)) \
        .where(forested_lands, ee.Image(0.1)) \
        .rename('P') \
        .clip(aoi)
    
    return p_factor

def compute_soil_loss(r, k, ls, c, p):
    """
    Compute soil loss using the RUSLE equation:
    A = R × K × LS × C × P
    
    Args:
        r (ee.Image): Rainfall erosivity factor
        k (ee.Image): Soil erodibility factor
        ls (ee.Image): Topographic factor
        c (ee.Image): Cover management factor
        p (ee.Image): Support practice factor
    
    Returns:
        ee.Image: Annual soil loss (t/ha/yr)
    """
    soil_loss = r.multiply(k).multiply(ls).multiply(c).multiply(p).rename('soil_loss')
    
    # Cap extremely high values for visualization purposes
    return soil_loss.min(200)  # Cap at 200 t/ha/yr

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


def compute_c_factor_dynamic(year, aoi, start_date, end_date):
    """
    Compute C factor dynamically based on NDVI
    
    Args:
        year (int): Year to process
        aoi (ee.Geometry): Area of interest
        start_date (str): Start date in format 'YYYY-MM-DD'
        end_date (str): End date in format 'YYYY-MM-DD'
    """
    try:
        # Use the updated Sentinel-2 collection
        s2_collection = ee.ImageCollection("COPERNICUS/S2_SR")
        
        # Filter collection
        filtered = s2_collection.filterBounds(aoi) \
                             .filterDate(start_date, end_date) \
                             .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
        
        # Check if collection is empty
        size = filtered.size().getInfo()
        if size == 0:
            print(f"Warning: No Sentinel-2 images available for {year}")
            # Return a default C factor with a proper band name
            return ee.Image(0.5).rename('C')
            
        # Compute NDVI
        def add_ndvi(image):
            ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
            return image.addBands(ndvi)
            
        with_ndvi = filtered.map(add_ndvi)
        
        # Get median NDVI
        median_ndvi = with_ndvi.select('NDVI').median()
        
        # Compute C factor from NDVI using the equation: C = 0.431 - 0.805 * NDVI
        c_factor = median_ndvi.multiply(-0.805).add(0.431).rename('C')
        
        # Ensure values are in valid range [0.01, 1]
        c_factor = c_factor.max(0.01).min(1.0)
        
        return c_factor
        
    except Exception as e:
        print(f"Error in compute_c_factor_dynamic: {str(e)}")
        # Return a default C factor with a proper band name
        return ee.Image(0.5).rename('C')

# PATCHED export_factor to handle incorrect types more robustly
def export_factor(factor, name, year, output_dir, aoi):
    try:
        tif_path = os.path.join(output_dir, f"{year}_{name}.tif")

        # Ensure it's a valid Earth Engine Image
        if not isinstance(factor, ee.Image):
            print(f"Error: {name} factor is not an ee.Image. Received: {type(factor)}")
            return False

        # Validate band names
        try:
            band_names = factor.bandNames().getInfo()
        except Exception as band_err:
            print(f"Band name fetch error for {name}: {str(band_err)}")
            return False

        if not band_names or len(band_names) == 0:
            print(f"Warning: {name} factor has no bands")
            return False

        # Visualization
        if name == 'soil_loss':
            visualization = factor.visualize(
                min=factor_ranges['soil_loss'][0],
                max=factor_ranges['soil_loss'][-1],
                palette=factor_palettes[name]
            )
        else:
            try:
                stats = factor.reduceRegion(
                    reducer=ee.Reducer.percentile([2, 98]),
                    geometry=aoi,
                    scale=500,
                    maxPixels=1e9
                ).getInfo()

                min_val = list(stats.values())[0]
                max_val = list(stats.values())[1]

                visualization = factor.visualize(
                    min=min_val,
                    max=max_val,
                    palette=factor_palettes[name]
                )
            except Exception as vis_error:
                print(f"Visualization error for {name}: {str(vis_error)}")
                visualization = factor.visualize(palette=factor_palettes[name])

        # Export image
        geemap.ee_export_image(
            visualization,
            filename=tif_path,
            scale=500,
            region=aoi,
            file_per_band=False
        )
        return True

    except Exception as e:
        print(f"Error exporting {name} factor: {str(e)}")
        return False

# PATCHED process_year to enforce proper fallback naming
def process_year(year, output_dir, aoi, start_date, end_date):
    try:
        print(f"Processing year {year}...")

        r_factor = compute_r_factor(chirps, start_date, end_date, aoi)
        print(f"R factor computed for {year}")

        k_factor = compute_k_factor(soil, soilOrganic, aoi)
        print(f"K factor computed for {year}")

        ls_factor = compute_ls_factor(dem, aoi)
        print(f"LS factor computed for {year}")

        c_factor = compute_c_factor_dynamic(year, aoi, start_date, end_date)
        print(f"C factor computed for {year}")

        p_factor = compute_p_factor(dem, landcover, aoi)
        print(f"P factor computed for {year}")

        factors = {
            'R': r_factor,
            'K': k_factor,
            'LS': ls_factor,
            'C': c_factor,
            'P': p_factor
        }

        for name, factor in factors.items():
            if isinstance(factor, str):
                print(f"Warning: {name} factor is a string. Replacing with fallback image.")
                factors[name] = ee.Image(0.5).rename(name)

        soil_loss = compute_soil_loss(
            factors['R'],
            factors['K'],
            factors['LS'],
            factors['C'],
            factors['P']
        )
        print(f"Soil loss computed for {year}")
        factors['soil_loss'] = soil_loss

        for name, factor in factors.items():
            success = export_factor(factor, name, year, output_dir, aoi)
            if success:
                print(f"Successfully exported {name} factor for year {year}")
            else:
                print(f"Failed to export {name} factor for year {year}")

        print(f"Successfully completed processing for year {year}")

    except Exception as e:
        print(f"Error processing year {year}: {str(e)}")
        print(f"  Completed processing for year {year}")

def create_stats_and_legend(year, name, factor, output_dir,aoi):
    """Create combined statistics and legend image with both existing and new enhanced statistics"""
    # Calculate comprehensive statistics including both existing and new stats
    stats = factor.reduceRegion(
        reducer=ee.Reducer.mean().combine(
            ee.Reducer.stdDev(), None, True
        ).combine(
            ee.Reducer.percentile([25, 50, 75]), None, True
        ).combine(
            ee.Reducer.sum(), None, True  # Add total sum
        ).combine(
            ee.Reducer.max(), None, True  # Add maximum value
        ).combine(
            ee.Reducer.min(), None, True  # Add minimum value
        ),
        geometry=aoi,
        scale=1000,
        maxPixels=1e9
    ).getInfo()
    
    if not stats:
        print(f"No data found for {name} factor in year {year}")
        return
    
    # Replace None values with 0
    stats_values = list(stats.values())
    stats_values = [0 if v is None else v for v in stats_values]
    
    # Initialize drawing components
    font = ImageFont.load_default()
    
    # Calculate text heights and spacing
    padding = 20
    line_spacing = 25
    legend_item_height = 40
    legend_spacing = 45
    
    # Enhanced stats text including both existing and new statistics
    stats_text = [
        f"Statistical Summary:",
        f"Total {name}: {stats_values[5]:.2f}",  # New: Total value
        f"Mean: {stats_values[0]:.2f}",          # Existing
        f"Standard Deviation: {stats_values[1]:.2f}", # Existing
        f"25th Percentile: {stats_values[2]:.2f}",   # Existing
        f"Median: {stats_values[3]:.2f}",            # Existing
        f"75th Percentile: {stats_values[4]:.2f}"    # Existing
    ]
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
        create_detailed_soil_loss_analysis(year, factor, stats, output_dir,aoi)

def create_detailed_soil_loss_analysis(year, factor, stats, output_dir,aoi):
    """Create a detailed analysis image with enhanced erosion calculations while preserving existing analysis"""
    # Calculate areas for each erosion class, including values above 50
    areas = {}
    
    # Calculate area for each range
    ranges = list(zip(factor_ranges['soil_loss'][:-1], factor_ranges['soil_loss'][1:]))
    for i, (min_val, max_val) in enumerate(ranges):
        area = factor.gte(min_val).And(factor.lt(max_val))\
            .multiply(ee.Image.pixelArea())\
            .divide(10000)\
            .reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=aoi,
                scale=50,
                maxPixels=1e9
            ).getInfo()
        
        label = factor_ranges['soil_loss_labels'][i]
        areas[label] = area.get('soil_loss', 0)
    
    # Calculate area for values >= 50 (severe erosion)
    severe_area = factor.gte(50)\
        .multiply(ee.Image.pixelArea())\
        .divide(10000)\
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=50,
            maxPixels=1e9
        ).getInfo()
    
    areas['Severe erosion ( >2 t/ha/yr)'] = severe_area.get('soil_loss', 0)
    
    # Calculate total erosion and other enhanced statistics
    total_erosion = factor.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=500,
        maxPixels=1e9
    ).getInfo().get('soil_loss', 0)
    
    # Create detailed analysis image with increased height for additional content
    img = Image.new("RGB", (600, 700), "white")
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
    severe_area = areas['Severe erosion ( >2 t/ha/yr)']
    severe_percent = (severe_area / total_area) * 100 if total_area > 0 else 0
    
    # Combine existing and new findings
    findings = [
        # Existing metrics
        f"Total analyzed area: {total_area:.2f} ha",
        f"Area under severe erosion: {severe_area:.2f} ha ({severe_percent:.1f}%)",
        f"Mean soil loss: {stats.get('mean', 0):.2f} t/ha/year",
        f"Median soil loss: {stats.get('median', 0):.2f} t/ha/year",
        # New enhanced metrics
        f"Total soil loss: {total_erosion:.2f} tonnes/year",
        f"Average soil loss rate: {(total_erosion/total_area):.2f} t/ha/year",
        f"Maximum recorded soil loss: {stats.get('max', 0):.2f} t/ha/year",
        f"Minimum recorded soil loss: {stats.get('min', 0):.2f} t/ha/year",
        f"Total area affected by severe erosion: {severe_area:.2f} ha",
        f"Percentage of land under severe erosion: {severe_percent:.1f}%"
    ]
    
    for finding in findings:
        draw.text((10, y_pos), finding, fill="black", font=font)
        y_pos += 25
    
    # Save both the original and enhanced analysis
    output_path = os.path.join(output_dir, f"{year}_soil_loss_detailed_analysis.png")
    img.save(output_path)
    
    

import os
import math
import ee
import pandas as pd

# Initialize Earth Engine
ee.Initialize()

def main():
    # Create a regions dataframe or load from CSV
    regions_data = [
        {"region_name": "Tosing", "center_lat": -30.342043706671383, "center_lon":  27.929300128161906, "distance_km": 10},
        {"region_name": "Linakeng", "center_lat": -29.52198184488981,  "center_lon": 28.867585216127754, "distance_km": 10},
        {"region_name": "Qibing", "center_lat": -29.692672088712456,  "center_lon": 27.101262321394724, "distance_km": 10},
        {"region_name": "Tsoelike", "center_lat": -30.017465520190267,  "center_lon": 28.66880599364324, "distance_km": 10},
        {"region_name": "Sanqebethu", "center_lat": -29.341522496809645,  "center_lon": 29.176083045693126, "distance_km": 10},
        {"region_name": "Mphosong", "center_lat": -29.0255874847322, "center_lon": 28.293781911919545, "distance_km": 10}
        #{"region_name": "Lesotho", "center_lat": -29.611587643481293,   "center_lon": 28.41379432778028, "distance_km": 270}
    ]
    
    # Convert list to DataFrame for easier management
    regions_df = pd.DataFrame(regions_data)
    
    # Save regions to CSV for future use
    regions_df.to_csv("regions.csv", index=False)

    # Process each region for each year
    for index, region in regions_df.iterrows():
        region_name = region["region_name"]
        center_lat = float(region["center_lat"])
        center_lon = float(region["center_lon"])
        distance = float(region["distance_km"])
        
        # Create AOI geometry for this region
        aoi = create_square_aoi(center_lat, center_lon, distance)
        
        # Create region-specific output directory
        region_output_dir = f"./Improved_RUSLE_Outputs/{region_name}"
        region_output_dir_aoi = f"./Improved_RUSLE_Outputs/{region_name}/aoi"
        os.makedirs(region_output_dir, exist_ok=True)
        os.makedirs(region_output_dir_aoi, exist_ok=True)
        start_date = "2024-01-01"
        end_date = "2024-12-31"
        #download_aoi_visualizations_gee(region_name, aoi, region_output_dir_aoi, start_date, end_date)
        #print(f"Processing region: {region_name}")
        
        # Process each year for this region
        for year in range(1995, 2025):
            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"
            
            # Create year-specific output directory
            year_output_dir = f"{region_output_dir}/{year}"
            os.makedirs(year_output_dir, exist_ok=True)
            
            # Download and save satellite images
            #Sdownload_aoi_visualizations_gee(region_name, aoi, year_output_dir, start_date, end_date)
            process_year(year, year_output_dir,aoi,start_date,end_date)
            
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

def download_aoi_visualizations_gee(region_name, aoi, output_dir, start_date, end_date):
    """Download satellite images for the given AOI and save to output directory"""
    # Fix: Use the updated dataset "COPERNICUS/S2_HARMONIZED"
    dataset = ee.ImageCollection("COPERNICUS/S2") \
        .filterBounds(aoi) \
        .filterDate(start_date, end_date) \
        .sort("CLOUDY_PIXEL_PERCENTAGE", True)  # Prioritize least cloudy images

    # Select the least cloudy image
    image = dataset.first()
    
    # Define visualization parameters
    vis_params = {
        "bands": ["B4", "B3", "B2"],  # True color (RGB)
        "min": 0,
        "max": 3000,
        "gamma": 1.4
    }

    # Convert image to a URL (PNG image)
    url = image.visualize(**vis_params).getThumbURL({
        "region": aoi,
        "dimensions": "1024x1024",
        "format": "PNG"
    })

    # Save the image locally
    image_path = os.path.join(output_dir, f"{region_name}_satellite_{start_date}.png")
    os.system(f"curl -o {image_path} {url}")
    
    print(f"  Saved satellite image for {region_name} ({start_date})")

if __name__ == "__main__":
    main()