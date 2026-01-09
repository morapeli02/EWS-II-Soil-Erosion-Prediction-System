import ee
import geemap
from PIL import Image, ImageDraw, ImageFont
import os
import math
import pandas as pd

##ee.Authenticate()
# Initialize Earth Engine
ee.Initialize(project="ee-makhosanemorapeli02")

# Define AOI



# Define datasets
chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/PENTAD")
dem = ee.Image("USGS/SRTMGL1_003")
landcover = landcover = ee.ImageCollection("ESA/WorldCover/v100").first()
soil = ee.Image("OpenLandMap/SOL/SOL_TEXTURE-CLASS_USDA-TT_M/v02")

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
    Compute Rainfall Erosivity Factor (R) using annual precipitation
    
    Equation: R = 79 + 0.363 × P
    Where:
    - R: Rainfall erosivity factor (MJ mm ha−1 h−1/y)
    - P: Annual rainfall (mm)
    
    Args:
        chirps (ee.ImageCollection): Precipitation dataset
        start_date (str): Start date of the analysis period
        end_date (str): End date of the analysis period
        aoi (ee.Geometry): Area of interest
    
    Returns:
        ee.Image: Rainfall Erosivity Factor
    """
    # Compute annual total precipitation
    annual_precipitation = (
        chirps
        .filter(ee.Filter.date(start_date, end_date))
        .reduce(ee.Reducer.sum())
        .clip(aoi)
    )
    
    # Apply R factor equation
    r_factor = annual_precipitation.expression(
        '79 + 0.363 * precipitation',
        {
            'precipitation': annual_precipitation
        }
    ).rename('R')
    
    return r_factor

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
                scale=500,
                shape='circular'
            ),
            r_factor
        )
    )
    
    return ee.Image(interpolated_r_factor).clip(aoi).rename('R')

def compute_k_factor(soil, aoi):
    # Use Earth Engine's expression to compute K factor dynamically based on soil values
    k_factor = soil.expression(
        "(b('b0') > 11) ? 0.0053 : " +
        "(b('b0') > 10) ? 0.0170 : " +
        "(b('b0') > 9) ? 0.045 : " +
        "(b('b0') > 8) ? 0.050 : " +
        "(b('b0') > 7) ? 0.0499 : " +
        "(b('b0') > 6) ? 0.0394 : " +
        "(b('b0') > 5) ? 0.0264 : " +
        "(b('b0') > 4) ? 0.0423 : " +
        "(b('b0') > 3) ? 0.0394 : " +
        "(b('b0') > 2) ? 0.036 : " +
        "(b('b0') > 1) ? 0.0341 : " +
        "(b('b0') > 0) ? 0.0288 : " +
        "0"
    ).rename('K').clip(aoi)
    
    return k_factor

def compute_ls_factor(dem, aoi):
    slope = ee.Terrain.slope(dem)
    slope_radians = slope.multiply(math.pi).divide(180)
    
    ls = slope_radians.expression(
        'pow(flowLength/22.13, 0.4) * pow(sin(slope)/0.0896, 1.3)',
        {
            'flowLength': ee.Image(50),
            'slope': slope_radians
        }
    ).clip(aoi)
    
    return ls.rename('LS')

#

# def get_dynamic_landcover(year, aoi):
#     """
#     Get MODIS NDVI data for a specific year
#     """
#     start_date = f"{year}-01-01"
#     end_date = f"{year}-12-31"
    
#     # Get MODIS NDVI collection
#     modis_ndvi = ee.ImageCollection("MODIS/006/MOD13Q1") \
#         .filter(ee.Filter.date(start_date, end_date)) \
#         .select('NDVI')
    
#     # Create annual composite
#     annual_ndvi = modis_ndvi.mean() \
#         .clip(aoi) \
#         .divide(10000)  # MODIS NDVI values are scaled by 10000
    
#     return annual_ndvi

def get_dynamic_landcover(year,aoi,start_date,end_date):
    """
    Get NDVI data from different satellites based on the year
    
    Years mapping:
    - 1995-2013: Landsat 5
    - 2014-2015: MODIS
    - 2015-present: Sentinel-2
    """
    
    try:
        if year >= 2015:
            # Use Sentinel-2 from 2015 to 2023
            collection = ee.ImageCollection("COPERNICUS/S2") \
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
                # Scale the bands according to metadata
                nir = image.select('SR_B4').multiply(0.0000275).add(-0.2)
                red = image.select('SR_B3').multiply(0.0000275).add(-0.2)
                ndvi = ee.Image.constant(0).expression(
                    '(nir - red) / (nir + red)', {
                        'nir': nir,
                        'red': red
                    }
                ).rename('NDVI')
                return image.addBands(ndvi)
            
            ndvi_collection = collection.map(calculate_landsat_ndvi)
            annual_ndvi = ndvi_collection.select('NDVI').mean()
        
        # Clip to area of interest and ensure valid NDVI range
        annual_ndvi = annual_ndvi.clip(aoi).max(-1).min(1)
        
        # Check if we have valid data
        count = ee.Image.constant(1).updateMask(annual_ndvi) \
            .reduceRegion(
                reducer=ee.Reducer.count(),
                geometry=aoi,
                scale=500,
                maxPixels=1e9
            ).get('constant').getInfo()
            
        if not count or count == 0:
            print(f"No valid NDVI data available for year {year}")
            return ee.Image.constant(0.3).clip(aoi).rename('NDVI')
            
        return annual_ndvi.rename('NDVI')
        
    except Exception as e:
        print(f"Error processing NDVI for year {year}: {str(e)}")
        return ee.Image.constant(0.3).clip(aoi).rename('NDVI')

def compute_c_factor_dynamic(year, aoi,start_date,end_date):
    """
    Compute C factor using NDVI from multiple satellite sources
    """
    try:
        ndvi = get_dynamic_landcover(year, aoi,start_date,end_date)
        
        # Ensure NDVI values are bounded
        ndvi = ndvi.max(-1).min(1)
        
        # Compute C factor using the provided equation
        c_factor = ndvi.expression(
            'min(max(0, (0.431 - 0.805 * ndvi) * exp(-(ndvi - 1.5) / 2.0)), 0.431)',
            {
                'ndvi': ndvi,
                'exp': 'Math.exp'
            }
        ).rename('C')
        
        return c_factor.clip(aoi)
        
    except Exception as e:
        print(f"Error computing C factor for year {year}: {str(e)}")
        return ee.Image.constant(0.3).clip(aoi).rename('C')

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


def compute_p_factor(slope, aoi):
    # P-factor based on slope
    return slope.expression(
        '(slope < 7) ? 0.55 : (slope < 11) ? 0.6 : (slope < 15) ? 0.7 : 0.8'
        , {'slope': slope}
    ).clip(aoi).rename('P')

def compute_soil_loss(r, k, ls, c, p):
    return r.multiply(k).multiply(ls).multiply(c).multiply(p).rename('soil_loss')

def process_year(year, output_dir,aoi,start_date,end_date):
    """
    Updated processing function with error handling
    """
    try:
        print(f"Processing year {year}...")
        
        # Compute dynamic factors
        r_factor = compute_r_factor(chirps, start_date, end_date, aoi)
        print(f"R factor computed for {year}")
        
        c_factor = compute_c_factor_dynamic(year, aoi,start_date,end_date)
        print(f"C factor computed for {year}")
        
        # Compute static factors
        k_factor = compute_k_factor(soil, aoi)
        print(f"K factor computed for {year}")
        
        ls_factor = compute_ls_factor(dem, aoi)
        print(f"LS factor computed for {year}")
        
        p_factor = compute_p_factor(ee.Terrain.slope(dem), aoi)
        print(f"P factor computed for {year}")
        
        # Compute soil loss
        soil_loss = compute_soil_loss(r_factor, k_factor, ls_factor, c_factor, p_factor)
        print(f"Soil loss computed for {year}")
        
        factors = {
            'R': r_factor,
            'K': k_factor,
            'LS': ls_factor,
            'C': c_factor,
            'P': p_factor,
            'soil_loss': soil_loss
        }
        
        # Export all factors with visualization
        for name, factor in factors.items():
            try:
                tif_path = os.path.join(output_dir, f"{year}_{name}.tif")
                
                # Ensure factor has data
                if factor.bandNames().size().getInfo() == 0:
                    print(f"Warning: {name} factor has no bands for year {year}")
                    continue
                
                if name == 'soil_loss':
                    visualization = factor.visualize(
                        min=factor_ranges['soil_loss'][0],
                        max=factor_ranges['soil_loss'][-1],
                        palette=factor_palettes[name]
                    )
                else:
                    # Get statistics for visualization
                    stats = factor.reduceRegion(
                        reducer=ee.Reducer.percentile([2, 98]),
                        geometry=aoi,
                        scale=500,
                        maxPixels=1e9
                    ).getInfo()
                    
                    if not stats:
                        print(f"Warning: Could not compute statistics for {name} factor in year {year}")
                        continue
                        
                    min_val = list(stats.values())[0]
                    max_val = list(stats.values())[1]
                    
                    visualization = factor.visualize(
                        min=min_val,
                        max=max_val,
                        palette=factor_palettes[name]
                    )
                
                geemap.ee_export_image(
                    visualization,
                    filename=tif_path,
                    scale=500,
                    region=aoi,
                    file_per_band=False
                )
                
                # Create enhanced statistics and legend
                create_stats_and_legend(year, name, factor, output_dir,aoi)
                
                print(f"Successfully exported {name} factor for year {year}")
                
            except Exception as e:
                print(f"Error processing {name} factor for year {year}: {str(e)}")
                continue
        
        print(f"Successfully completed processing for year {year}")
        
    except Exception as e:
        print(f"Error processing year {year}: {str(e)}")

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
    
    areas['Severe erosion ( >50 t/ha/yr)'] = severe_area.get('soil_loss', 0)
    
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
    severe_area = areas['Severe erosion ( >50 t/ha/yr)']
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
    
    

def download_aoi_visualizations_gee(region_name, aoi, output_dir):
    """Download different views of the AOI using GEE only"""
    # Create visualization directory
    viz_dir = os.path.join(output_dir, "aoi_visualization")
    os.makedirs(viz_dir, exist_ok=True)
    
    try:
        # 1. Satellite view (using Landsat)
        satellite_img = ee.ImageCollection('LANDSAT/LC08/C02/T1_TOA') \
            .filterBounds(aoi) \
            .sort('CLOUD_COVER') \
            .first() \
            .select(['B4', 'B3', 'B2'])
        
        # Create true color visualization
        satellite_vis = satellite_img.visualize(
            min=0.0,
            max=0.3,
            bands=['B4', 'B3', 'B2']
        )
        
        # Export satellite view
        satellite_path = os.path.join(viz_dir, f"{region_name}_satellite.tif")
        geemap.ee_export_image(
            satellite_vis,
            filename=satellite_path,
            scale=500,
            region=aoi,
            file_per_band=False
        )
        print(f"Satellite image saved to {satellite_path}")
        
        # 2. Terrain view (using SRTM DEM)
        dem = ee.Image('USGS/SRTMGL1_003').clip(aoi)
        terrain_vis = ee.Terrain.hillshade(dem).visualize(
            min=0,
            max=255
        )
        
        # Export terrain view
        terrain_path = os.path.join(viz_dir, f"{region_name}_terrain.tif")
        geemap.ee_export_image(
            terrain_vis,
            filename=terrain_path,
            scale=500,
            region=aoi,
            file_per_band=False
        )
        print(f"Terrain image saved to {terrain_path}")
        
        # 3. Map/Roads view (using OSM)
        # For roads, we can use the ESA WorldCover dataset which shows land use
        # This isn't exactly roads but shows human development
        worldcover = ee.ImageCollection("ESA/WorldCover/v100").first().clip(aoi)
        
        # Visualization parameters for WorldCover
        vis_params = {
            'bands': ['Map'],
        }
        
        roads_path = os.path.join(viz_dir, f"{region_name}_landuse.tif")
        geemap.ee_export_image(
            worldcover.visualize(**vis_params),
            filename=roads_path,
            scale=500,
            region=aoi,
            file_per_band=False
        )
        print(f"Land use image saved to {roads_path}")
        
    except Exception as e:
        print(f"Error downloading AOI visualizations: {str(e)}")
   


def main():
    # Create a regions dataframe or load from CSV
    regions_data = [
        {"region_name": "Tosing", "center_lat": -30.342043706671383, "center_lon":  27.929300128161906, "distance_km": 10},
        {"region_name": "Linakeng", "center_lat": -29.52198184488981,  "center_lon": 28.867585216127754, "distance_km": 10},
        {"region_name": "Qibing", "center_lat": -29.692672088712456,  "center_lon": 27.101262321394724, "distance_km": 10},
        {"region_name": "Tsoelike", "center_lat": -30.017465520190267,  "center_lon": 28.66880599364324, "distance_km": 10},
        {"region_name": "Sanqebethu", "center_lat": -29.341522496809645,  "center_lon": 29.176083045693126, "distance_km": 10},
        {"region_name": "Mphosong", "center_lat": -29.0255874847322, "center_lon": 28.293781911919545, "distance_km": 10},
        {"region_name": "Lesotho", "center_lat": -29.611587643481293,   "center_lon": 28.41379432778028, "distance_km": 270}
        # Add more regions as needed
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
            
            process_year(year, year_output_dir, aoi, start_date, end_date)
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



if __name__ == "__main__":
    main()
    
