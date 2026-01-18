import ee
import geemap
import math
import os
import requests
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

# Initialize Earth Engine
ee.Initialize(project="ee-makhosanemorapeli02")

# --- PALETTES & LABELS (Aligned with your style) ---
mmf_palettes = {
    'MMF_Soil_Loss': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
    'Runoff': ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#4292c6", "#084594"],
    'Detachment': ["#fee5d9", "#fcae91", "#fb6a4a", "#de2d26", "#a50f15"]
}

loss_labels = [
    "Very Low (<1 Mg/ha/yr)",
    "Low (1-5 Mg/ha/yr)",
    "Moderate (5-10 Mg/ha/yr)",
    "High (10-20 Mg/ha/yr)",
    "Severe (>20 Mg/ha/yr)"
]

# --- CORE MODEL LOGIC ---

def get_mmf_inputs(aoi, start_date, end_date):
    """Robust data fetching for MMF."""
    precip = ee.ImageCollection("UCSB-CHG/CHIRPS/PENTAD") \
        .filterDate(start_date, end_date).sum().clip(aoi)
    
    dem = ee.Image("USGS/SRTMGL1_003").clip(aoi)
    slope = ee.Terrain.slope(dem).multiply(math.pi / 180)
    
    # Sentinel-2 with fallback for empty/cloudy collections
    s2_col = ee.ImageCollection("COPERNICUS/S2_HARMONIZED") \
        .filterBounds(aoi).filterDate(start_date, end_date) \
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENT', 30))
    
    s2_image = ee.Image(ee.Algorithms.If(s2_col.size().gt(0), s2_col.median(), ee.Image.constant(0)))
    
    # FIX: Check for B8 before NDVI calculation to prevent 'No band named B8' error
    ndvi = ee.Image(ee.Algorithms.If(
        s2_image.bandNames().contains('B8'),
        s2_image.normalizedDifference(['B8', 'B4']),
        ee.Image.constant(0.3)
    )).rename('NDVI').clip(aoi)
    
    soil_org = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02").clip(aoi).select('b0')
    
    return precip, slope, ndvi, soil_org

def run_mmf_model(aoi, year):
    """Physical model implementation with fixed expression variables."""
    start_date = f'{year}-01-01'
    end_date = f'{year}-12-31'
    precip, slope, ndvi, soil_org = get_mmf_inputs(aoi, start_date, end_date)

    # Canopy Cover (CC) - FIX: Explicitly mapped 'ndvi' variable
    cc = ndvi.expression('(ndvi - 0.1) / (0.9 - 0.1)', {'ndvi': ndvi}).clamp(0, 0.95).rename('CC')
    gc = cc.multiply(0.8).rename('GC') 

    # Water Phase
    interception = cc.multiply(0.25)
    effective_precip = precip.multiply(ee.Image(1).subtract(interception))
    ke = effective_precip.multiply(11.9 + 8.7 * 0.5)
    
    rc = ee.Image.constant(100) 
    # Runoff (Q) - FIX: Explicitly mapped 'p' and 'rc'
    runoff = effective_precip.expression(
        'p * exp(-rc / p)', 
        {'p': effective_precip, 'rc': rc}
    ).rename('Runoff')

    # Sediment Phase
    k_mmf = ee.Image.constant(0.05).where(soil_org.gt(20), 0.02) 
    det_rain = k_mmf.multiply(ke).multiply(0.001)
    
    z = ee.Image.constant(0.01)
    det_runoff = z.multiply(runoff.pow(1.5)).multiply(slope.sin()).multiply(ee.Image(1).subtract(gc)).multiply(0.001)
    total_detachment = det_rain.add(det_runoff).rename('Detachment')

    # Transport Capacity
    c_factor = ee.Image.constant(1).subtract(cc).multiply(0.5)
    tc = c_factor.multiply(runoff.pow(2)).multiply(slope.sin()).multiply(0.001).rename('Capacity')

    soil_loss = total_detachment.min(tc).rename('MMF_Soil_Loss')

    return {'MMF_Soil_Loss': soil_loss, 'Runoff': runoff, 'Detachment': total_detachment}

# --- VISUALIZATION & FILE SAVING ---

def create_mmf_summary_image(year, name, factor, output_dir, aoi):
    """Creates the combined stats and legend PNG."""
    stats = factor.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.max(), "", True).combine(ee.Reducer.min(), "", True),
        geometry=aoi, scale=30, maxPixels=1e9
    ).getInfo()

    img = Image.new("RGB", (550, 450), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    current_y = 20
    draw.text((20, current_y), f"MMF ANALYSIS: {name} ({year})", fill="black", font=font)
    current_y += 40

    # Draw stats
    stat_lines = [
        f"Mean: {stats.get(f'{name}_mean', 0):.4f}",
        f"Max: {stats.get(f'{name}_max', 0):.4f}",
        f"Min: {stats.get(f'{name}_min', 0):.4f}",
        "", "Legend:"
    ]
    
    for line in stat_lines:
        draw.text((20, current_y), line, fill="black", font=font)
        current_y += 25

    # Draw Legend Boxes
    palette = mmf_palettes.get(name, ["#000000"])
    for i, color in enumerate(palette):
        draw.rectangle([20, current_y, 60, current_y + 25], fill=color, outline="black")
        if name == 'MMF_Soil_Loss' and i < len(loss_labels):
            draw.text((75, current_y + 5), loss_labels[i], fill="black", font=font)
        current_y += 35

    img.save(os.path.join(output_dir, f"{year}_{name}_stats.png"))

def process_mmf_region(region, year):
    """Handles directory creation and export for each region/year."""
    name = region['region_name']
    lat, lon, dist = region['center_lat'], region['center_lon'], region['distance_km']
    
    # Calculate AOI bounds
    lat_offset = (dist/2) / 111.32
    lon_offset = (dist/2) / (111.32 * math.cos(math.radians(lat)))
    aoi = ee.Geometry.Polygon([[
        [lon - lon_offset, lat - lat_offset], [lon - lon_offset, lat + lat_offset],
        [lon + lon_offset, lat + lat_offset], [lon + lon_offset, lat - lat_offset],
        [lon - lon_offset, lat - lat_offset]
    ]])

    output_path = f"./MMF_Outputs/{name}/{year}"
    os.makedirs(output_path, exist_ok=True)

    results = run_mmf_model(aoi, year)

    for factor_name, img in results.items():
        # Export Styled Geotiff
        vis_params = {'min': 0, 'max': 20 if 'Loss' in factor_name else 500, 'palette': mmf_palettes.get(factor_name)}
        vis_img = img.visualize(**vis_params)
        geemap.ee_export_image(vis_img, filename=os.path.join(output_path, f"{year}_{factor_name}.tif"), scale=30, region=aoi)
        
        # Save Stats/Legend PNG
        create_mmf_summary_image(year, factor_name, img, output_path, aoi)

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    # You can add your full list of regions here
    regions_list = [
        {"region_name": "Koalabata", "center_lat": -29.2935, "center_lon": 27.5588, "distance_km": 5}
    ]
    
    for region in regions_list:
        # Update range to process multiple years if needed
        for year in range(2023, 2024):
            process_mmf_region(region, year)
    
    print("MMF Analysis and Visualization Complete.")