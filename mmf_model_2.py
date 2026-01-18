import ee
import geemap
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
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

def get_base_data(aoi, year):
    """Fetch base data layers that don't change daily."""
    start_date = f'{year}-01-01'
    end_date = f'{year}-12-31'
    
    # Elevation and Slope (in radians)
    dem = ee.Image("USGS/SRTMGL1_003").clip(aoi)
    slope = ee.Terrain.slope(dem).multiply(math.pi / 180).rename('slope')
    
    # Soil Data
    soil_org = ee.Image("OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02") \
        .clip(aoi).select('b0').multiply(0.1).rename('soil_org')  # Convert to g/kg
    
    soil_clay = ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02") \
        .clip(aoi).select('b0').multiply(0.1).rename('clay')  # Convert to %
    
    # CALIBRATED K-factor for Lesotho (kg/J) - INCREASED
    # Previous: 0.01-0.04, New: 0.03-0.08 (more realistic for degraded grasslands)
    k_mmf = ee.Image.constant(0.06) \
        .where(soil_org.gt(30), 0.03) \
        .where(soil_clay.lt(10), 0.08) \
        .rename('k_mmf')
    
    # Soil moisture storage capacity (Rc, mm)
    # Sandy: 50-80, Loamy: 80-120, Clay: 120-150
    rc = ee.Image.constant(80) \
        .where(soil_clay.gt(35), 120) \
        .where(soil_clay.lt(15), 60) \
        .rename('rc')
    
    return {
        'dem': dem,
        'slope': slope,
        'k_mmf': k_mmf,
        'rc': rc,
        'soil_org': soil_org
    }

def get_vegetation_params(aoi, date):
    """Get vegetation parameters for a specific date (monthly composite)."""
    # Extended window for better data availability
    start = ee.Date(date).advance(-30, 'day')
    end = ee.Date(date).advance(30, 'day')
    
    # Primary: Try MODIS Terra 16-day NDVI (most reliable for any location)
    # Available globally from 2000 onwards
    try:
        modis = ee.ImageCollection("MODIS/061/MOD13A1") \
            .filterBounds(aoi) \
            .filterDate(start, end) \
            .select('NDVI') \
            .mean() \
            .clip(aoi)
        
        # MODIS NDVI is scaled by 10000
        ndvi = modis.multiply(0.0001).rename('NDVI')
        
    except:
        # Ultimate fallback: Use Landsat 8/9 NDVI
        landsat = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2") \
            .filterBounds(aoi) \
            .filterDate(start, end) \
            .filter(ee.Filter.lt('CLOUD_COVER', 30)) \
            .select(['SR_B5', 'SR_B4']) \
            .mean() \
            .clip(aoi)
        
        # Landsat NDVI from B5 (NIR) and B4 (Red)
        ndvi = landsat.normalizedDifference(['SR_B5', 'SR_B4']).rename('NDVI')
    
    # Ensure NDVI is valid
    ndvi = ndvi.unmask(0.3)  # Default to moderate vegetation if no data
    
    # Canopy Cover (CC): 0-1, derived from NDVI
    cc = ndvi.expression(
        '(ndvi - 0.05) / (0.85 - 0.05)',
        {'ndvi': ndvi}
    ).clamp(0, 0.95).rename('CC')
    
    # Ground Cover (GC): Includes litter and low vegetation
    gc = cc.multiply(0.6).add(0.1).clamp(0, 0.95).rename('GC')
    
    # Plant Height (m) - Conservative for Lesotho grasslands
    ph = ee.Image.constant(0.2) \
        .where(ndvi.gt(0.5), 0.35) \
        .where(ndvi.gt(0.7), 0.6) \
        .rename('PH')
    
    # C-factor for MMF (vegetation management)
    c_factor = ee.Image.constant(1).subtract(cc).multiply(0.4).add(0.05).rename('C')
    
    return {
        'ndvi': ndvi,
        'cc': cc,
        'gc': gc,
        'ph': ph,
        'c_factor': c_factor
    }

def calculate_daily_mmf(precip, intensity, base_data, veg_params, soil_moisture):
    """
    Calculate MMF for a single day.
    
    Args:
        precip: Daily rainfall (mm)
        intensity: Rainfall intensity (mm/hr)
        base_data: Dict with slope, k_mmf, rc
        veg_params: Dict with cc, gc, ph, c_factor
        soil_moisture: Antecedent soil moisture (mm)
    """
    
    # --- 1. WATER PHASE ---
    
    # Permanent interception (MS, mm) - water stored on leaves
    ms = veg_params['ph'].multiply(0.1).multiply(veg_params['cc']).rename('MS')
    
    # Interception rate (A) - fraction of rain intercepted
    # A = 0.03 * CC for low intensity, increases with intensity
    a_intercept = veg_params['cc'].multiply(0.03).multiply(
        ee.Image.constant(1).add(intensity.divide(10))
    ).clamp(0, 0.4).rename('A')
    
    # Effective rainfall (mm)
    effective_precip = precip.multiply(
        ee.Image.constant(1).subtract(a_intercept)
    ).subtract(ms).max(0).rename('P_eff')
    
    # Kinetic Energy of Rainfall (J/m²)
    # KE = P × (11.9 + 8.7 × log₁₀(I))
    # Note: I must be > 0, use 0.1 mm/hr minimum
    safe_intensity = intensity.max(0.1)
    ke = effective_precip.multiply(
        ee.Image.constant(11.9).add(
            safe_intensity.log10().multiply(8.7)
        )
    ).rename('KE')
    
    # Runoff (Q, mm)
    # Q = P × e^(-Rc/P) when P > 0, considering soil moisture
    # Adjusted Rc based on current soil moisture
    rc_adj = base_data['rc'].subtract(soil_moisture).max(10)
    
    runoff = effective_precip.expression(
        'P > 0 ? P * (1 - exp(-P / Rc)) : 0',
        {
            'P': effective_precip,
            'Rc': rc_adj
        }
    ).rename('Q')
    
    # --- 2. SEDIMENT PHASE: DETACHMENT ---
    
    # Detachment by Rainsplash (F, kg/m²)
    # F = K × KE × (1 - GC)² 
    # MULTIPLIER INCREASED: was 1e-3, now 2e-3 to account for actual field conditions
    detach_rain = base_data['k_mmf'].multiply(ke).multiply(
        ee.Image.constant(1).subtract(veg_params['gc']).pow(2)
    ).multiply(2e-3).rename('F')  # Adjusted multiplier
    
    # Detachment by Runoff (H, kg/m²)
    # H = Z × Q^1.5 × sin(S) × (1 - GC)
    # Z = soil resistance - INCREASED from 0.001-0.005 to 0.003-0.015
    z = ee.Image.constant(0.008) \
        .where(base_data['k_mmf'].gt(0.05), 0.015) \
        .rename('Z')
    
    detach_runoff = z.multiply(runoff.pow(1.5)) \
        .multiply(base_data['slope'].sin()) \
        .multiply(ee.Image.constant(1).subtract(veg_params['gc'])) \
        .multiply(2e-3).rename('H')  # Adjusted multiplier
    
    total_detachment = detach_rain.add(detach_runoff).rename('D_total')
    
    # --- 3. SEDIMENT PHASE: TRANSPORT CAPACITY ---
    
    # Transport Capacity (TC, kg/m²)
    # TC = C × Q² × sin(S) × ρ
    # ρ = sediment density (~1500 kg/m³ for mineral soil)
    # INCREASED MULTIPLIER: was 1.5e-3, now 3e-3 for better calibration
    transport_cap = veg_params['c_factor'] \
        .multiply(runoff.pow(2)) \
        .multiply(base_data['slope'].sin()) \
        .multiply(3e-3).rename('TC')  # Adjusted for field conditions
    
    # --- 4. FINAL SOIL LOSS ---
    
    # Soil loss is minimum of detachment and transport capacity
    soil_loss = total_detachment.min(transport_cap).rename('SL')
    
    # Update soil moisture for next day (simplified)
    new_soil_moisture = soil_moisture.add(effective_precip).subtract(runoff) \
        .clamp(0, base_data['rc'])
    
    return {
        'soil_loss': soil_loss,
        'runoff': runoff,
        'detachment': total_detachment,
        'transport_cap': transport_cap,
        'ke': ke,
        'soil_moisture': new_soil_moisture
    }

def run_daily_mmf(aoi, year, validation_data=None):
    """
    Run MMF on daily timestep and aggregate results.
    
    Args:
        aoi: Area of interest
        year: Year to analyze
        validation_data: Optional dict with measured data for comparison
    """
    
    print(f"Processing MMF for {year}...")
    
    # Get base data
    base_data = get_base_data(aoi, year)
    
    # Get daily precipitation
    start_date = f'{year}-01-01'
    end_date = f'{year}-12-31'
    
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY") \
        .filterDate(start_date, end_date) \
        .filterBounds(aoi)
    
    # Get list of all dates (server-side)
    dates_list = chirps.aggregate_array('system:time_start')
    
    # Process with monthly vegetation updates (server-side only)
    # Create ee.Date objects properly on server side
    year_ee = ee.Number(year)
    start_ee = ee.Date(start_date)
    
    # Function to process each day
    def process_daily(date_millis):
        """Process a single day - fully server-side."""
        date = ee.Date(date_millis)
        
        # Get daily precipitation
        daily_precip = chirps.filterDate(date, date.advance(1, 'day')).first()
        precip = daily_precip.select('precipitation')
        
        # Estimate rainfall intensity (mm/hr)
        # Higher intensity for higher daily totals
        # I ≈ P / duration, typical storm: 2-6 hours
        intensity = precip.expression(
            'P > 20 ? P / 3 : (P > 5 ? P / 4 : P / 2)',
            {'P': precip}
        ).max(0.1)
        
        # Get vegetation (use monthly composite for efficiency)
        # Round to nearest 15th of month
        month = date.get('month')
        veg_date = ee.Date.fromYMD(date.get('year'), month, 15)
        veg = get_vegetation_params(aoi, veg_date)
        
        # Initialize soil moisture (simplified - in reality would carry forward)
        soil_moisture = base_data['rc'].multiply(0.5)
        
        # Calculate daily MMF
        result = calculate_daily_mmf(
            precip,
            intensity,
            base_data,
            veg,
            soil_moisture
        )
        
        return result['soil_loss']
    
    # Map over all days and sum (this can be slow for full year)
    # For large areas or long periods, consider monthly aggregation
    print("Processing daily calculations...")
    
    # Alternative: Process monthly batches for efficiency
    # This is the practical approach for GEE
    monthly_results = []
    
    for month in range(1, 13):
        month_start = f'{year}-{month:02d}-01'
        # Get last day of month
        if month == 12:
            month_end = f'{year + 1}-01-01'
        else:
            month_end = f'{year}-{month + 1:02d}-01'
        
        print(f"  Processing month {month}/12...")
        
        # Get monthly precipitation
        monthly_chirps = chirps.filterDate(month_start, month_end)
        monthly_precip = monthly_chirps.sum().clip(aoi)
        
        # Get vegetation for mid-month
        mid_month_date = f'{year}-{month:02d}-15'
        monthly_veg = get_vegetation_params(aoi, ee.Date(mid_month_date))
        
        # Estimate monthly average intensity
        # Count rainy days (P > 1mm)
        rainy_days = monthly_chirps.map(
            lambda img: img.select('precipitation').gt(1)
        ).sum()
        
        # CALIBRATED: Higher intensity for Lesotho storms
        # Average intensity = total P / (rainy days * avg storm duration)
        # Increased from 4 hours to 3 hours (more intense storms)
        # Also increased minimum from 5 to 12 mm/hr
        avg_intensity = monthly_precip.divide(rainy_days.multiply(3)).max(12)
        
        # Calculate for this month
        soil_moisture = base_data['rc'].multiply(0.5)
        month_result = calculate_daily_mmf(
            monthly_precip,
            avg_intensity,
            base_data,
            monthly_veg,
            soil_moisture
        )
        
        monthly_results.append(month_result['soil_loss'])
    
    # Sum all monthly results
    annual_soil_loss = ee.ImageCollection(monthly_results).sum()
    
    # Also calculate annual approach for comparison
    annual_precip = chirps.sum().clip(aoi)
    avg_intensity = ee.Image.constant(15)  # Increased from 10 to 15 mm/hr
    mid_year_veg = get_vegetation_params(aoi, ee.Date(f'{year}-06-15'))
    soil_moisture = base_data['rc'].multiply(0.5)
    
    annual_result = calculate_daily_mmf(
        annual_precip,
        avg_intensity,
        base_data,
        mid_year_veg,
        soil_moisture
    )
    
    # Convert to annual rates (kg/m² → Mg/ha)
    # Use monthly aggregation result
    soil_loss_annual = annual_soil_loss.multiply(10).rename('MMF_Monthly_Aggregated')
    
    # Also keep simple annual calculation for comparison
    soil_loss_simple = annual_result['soil_loss'].multiply(10).rename('MMF_Annual_Simple')
    
    # Use monthly aggregation as primary result (more accurate)
    primary_result = soil_loss_annual
    
    # Calculate statistics
    stats = primary_result.reduceRegion(
        reducer=ee.Reducer.mean().combine(
            ee.Reducer.median(), '', True
        ).combine(
            ee.Reducer.stdDev(), '', True
        ).combine(
            ee.Reducer.max(), '', True
        ),
        geometry=aoi,
        scale=30,
        maxPixels=1e9
    ).getInfo()
    
    print(f"\n{'='*60}")
    print(f"MMF Results for {year} (Monthly Aggregation Method)")
    print(f"{'='*60}")
    print(f"Mean Soil Loss:   {stats.get('MMF_Monthly_Aggregated_mean', 0):.2f} Mg/ha/yr")
    print(f"Median Soil Loss: {stats.get('MMF_Monthly_Aggregated_median', 0):.2f} Mg/ha/yr")
    print(f"Std Dev:          {stats.get('MMF_Monthly_Aggregated_stdDev', 0):.2f} Mg/ha/yr")
    print(f"Maximum:          {stats.get('MMF_Monthly_Aggregated_max', 0):.2f} Mg/ha/yr")
    
    # Show calibration parameters used
    print(f"\n{'='*60}")
    print("CALIBRATION PARAMETERS USED")
    print(f"{'='*60}")
    print("K-factor (soil erodibility): 0.03-0.08 kg/J")
    print("Z-factor (soil resistance): 0.008-0.015 kg/m³")
    print("Rainfall intensity: 12-20 mm/hr (storm average)")
    print("Detachment multiplier: 2e-3 (calibrated for Lesotho)")
    print("Transport multiplier: 3e-3 (calibrated for Lesotho)")
    
    # Validation comparison
    if validation_data:
        print(f"\n{'='*60}")
        print("VALIDATION COMPARISON")
        print(f"{'='*60}")
        
        measured = validation_data.get('measured_loss', 0)
        modeled = stats.get('MMF_Monthly_Aggregated_mean', 0)
        
        if measured > 0:
            error = abs(modeled - measured) / measured * 100
            print(f"Measured Soil Loss:     {measured:.2f} Mg/ha/yr")
            print(f"Modeled Soil Loss:      {modeled:.2f} Mg/ha/yr")
            print(f"Relative Error:         {error:.1f}%")
            
            if error < 20:
                print("✓ Good agreement (< 20% error)")
            elif error < 40:
                print("~ Moderate agreement (20-40% error)")
            else:
                print("✗ Poor agreement (> 40% error)")
                print("  Consider adjusting: K-factor, intensity, or vegetation params")
    
    # Comparison with RUSLE if provided
    if 'rusle_loss' in validation_data:
        rusle = validation_data['rusle_loss']
        print(f"\nComparison with RUSLE:")
        print(f"RUSLE:  {rusle:.2f} Mg/ha/yr")
        print(f"MMF:    {modeled:.2f} Mg/ha/yr")
        print(f"Ratio (MMF/RUSLE): {modeled/rusle:.2f}")
        print("\nNote: MMF typically estimates 0.5-1.5x RUSLE values")
        print("      Lower ratios suggest conservative parameters")
    
    return {
        'soil_loss': primary_result,
        'runoff': annual_result['runoff'],
        'stats': stats,
        'base_data': base_data,
        'veg_params': mid_year_veg,
        'monthly_losses': monthly_results
    }

def visualize_results(aoi, results, year, output_dir='outputs'):
    """Create and save map visualizations of results."""
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    Map = geemap.Map()
    Map.centerObject(aoi, 13)
    
    # Soil loss visualization
    loss_vis = {
        'min': 0,
        'max': 25,
        'palette': ['#006d2c', '#31a354', '#74c476', '#fed976', '#fd8d3c', '#e31a1c', '#800026']
    }
    
    Map.addLayer(results['soil_loss'], loss_vis, 'MMF Soil Loss (Mg/ha/yr)')
    
    # Runoff visualization
    runoff_vis = {
        'min': 0,
        'max': 300,
        'palette': ['#f7fbff', '#deebf7', '#9ecae1', '#4292c6', '#08519c']
    }
    Map.addLayer(results['runoff'], runoff_vis, 'Annual Runoff (mm)')
    
    # Add NDVI layer
    ndvi_vis = {
        'min': 0,
        'max': 0.8,
        'palette': ['#d73027', '#fee08b', '#d9ef8b', '#91cf60', '#1a9850']
    }
    Map.addLayer(results['veg_params']['ndvi'], ndvi_vis, 'NDVI')
    
    # Add legend
    Map.add_colorbar(loss_vis, label='Soil Loss (Mg/ha/yr)', position='bottomright')
    
    # Save interactive HTML map
    map_file = os.path.join(output_dir, f'MMF_Map_{year}.html')
    Map.save(map_file)
    print(f"✓ Interactive map saved: {map_file}")
    
    return Map

def export_map_images(aoi, results, year, output_dir='outputs', region_name='Region'):
    """
    Export map visualizations as PNG images (similar to your RUSLE approach).
    
    Args:
        aoi: Area of interest
        results: Results dict from run_daily_mmf
        year: Year of analysis
        output_dir: Output directory
        region_name: Name of region for filename
    """
    
    os.makedirs(output_dir, exist_ok=True)
    
    # Get bounding box for export
    bounds = aoi.bounds().getInfo()['coordinates'][0]
    
    # Define visualization parameters
    loss_vis = {
        'min': 0,
        'max': 25,
        'palette': ['006d2c', '31a354', '74c476', 'fed976', 'fd8d3c', 'e31a1c', '800026']
    }
    
    runoff_vis = {
        'min': 0,
        'max': 300,
        'palette': ['f7fbff', 'deebf7', '9ecae1', '4292c6', '08519c']
    }
    
    ndvi_vis = {
        'min': 0,
        'max': 0.8,
        'palette': ['d73027', 'fee08b', 'd9ef8b', '91cf60', '1a9850']
    }
    
    print(f"\nExporting map images for {region_name} ({year})...")
    
    # 1. Export Soil Loss Map
    print("  → Exporting soil loss map...")
    loss_url = results['soil_loss'].visualize(**loss_vis).getThumbURL({
        'region': aoi,
        'dimensions': 1024,
        'format': 'png'
    })
    
    loss_filename = os.path.join(output_dir, f'MMF_SoilLoss_{region_name}_{year}.png')
    geemap.download_file(loss_url, loss_filename)
    print(f"    ✓ Saved: {loss_filename}")
    
    # 2. Export Runoff Map
    print("  → Exporting runoff map...")
    runoff_url = results['runoff'].visualize(**runoff_vis).getThumbURL({
        'region': aoi,
        'dimensions': 1024,
        'format': 'png'
    })
    
    runoff_filename = os.path.join(output_dir, f'MMF_Runoff_{region_name}_{year}.png')
    geemap.download_file(runoff_url, runoff_filename)
    print(f"    ✓ Saved: {runoff_filename}")
    
    # 3. Export NDVI Map
    print("  → Exporting NDVI map...")
    ndvi_url = results['veg_params']['ndvi'].visualize(**ndvi_vis).getThumbURL({
        'region': aoi,
        'dimensions': 1024,
        'format': 'png'
    })
    
    ndvi_filename = os.path.join(output_dir, f'MMF_NDVI_{region_name}_{year}.png')
    geemap.download_file(ndvi_url, ndvi_filename)
    print(f"    ✓ Saved: {ndvi_filename}")
    
    # 4. Create a composite image with titles and legends
    print("  → Creating composite visualization...")
    create_composite_map(
        loss_filename, 
        runoff_filename, 
        ndvi_filename,
        output_dir, 
        region_name, 
        year,
        results['stats']
    )
    
    return {
        'soil_loss_map': loss_filename,
        'runoff_map': runoff_filename,
        'ndvi_map': ndvi_filename
    }

def create_composite_map(loss_img, runoff_img, ndvi_img, output_dir, region_name, year, stats):
    """Create a composite image with all three maps and statistics."""
    
    try:
        # Load images
        img_loss = Image.open(loss_img)
        img_runoff = Image.open(runoff_img)
        img_ndvi = Image.open(ndvi_img)
        
        # Create composite (3 columns)
        width, height = img_loss.size
        composite_width = width * 3 + 80  # 40px padding between images
        composite_height = height + 200   # Extra space for titles and stats
        
        composite = Image.new('RGB', (composite_width, composite_height), 'white')
        
        # Paste images
        composite.paste(img_loss, (20, 80))
        composite.paste(img_runoff, (width + 40, 80))
        composite.paste(img_ndvi, (width * 2 + 60, 80))
        
        # Add text
        draw = ImageDraw.Draw(composite)
        
        # Try to use a better font, fall back to default if not available
        try:
            title_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
            subtitle_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
            stats_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
        except:
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default()
            stats_font = ImageFont.load_default()
        
        # Main title
        main_title = f"MMF Soil Erosion Analysis - {region_name} ({year})"
        draw.text((composite_width // 2 - 300, 20), main_title, fill='black', font=title_font)
        
        # Subtitles
        draw.text((width // 2 - 100, 50), "Soil Loss (Mg/ha/yr)", fill='black', font=subtitle_font)
        draw.text((width + 40 + width // 2 - 80, 50), "Runoff (mm)", fill='black', font=subtitle_font)
        draw.text((width * 2 + 60 + width // 2 - 40, 50), "NDVI", fill='black', font=subtitle_font)
        
        # Statistics at bottom
        stats_y = height + 100
        mean_loss = stats.get('MMF_Monthly_Aggregated_mean', 0)
        max_loss = stats.get('MMF_Monthly_Aggregated_max', 0)
        median_loss = stats.get('MMF_Monthly_Aggregated_median', 0)
        
        stats_text = [
            f"Mean Soil Loss: {mean_loss:.2f} Mg/ha/yr",
            f"Median: {median_loss:.2f} Mg/ha/yr",
            f"Maximum: {max_loss:.2f} Mg/ha/yr"
        ]
        
        for i, text in enumerate(stats_text):
            draw.text((40, stats_y + i * 25), text, fill='black', font=stats_font)
        
        # Save composite
        composite_filename = os.path.join(output_dir, f'MMF_Composite_{region_name}_{year}.png')
        composite.save(composite_filename, dpi=(300, 300))
        print(f"    ✓ Saved composite: {composite_filename}")
        
        return composite_filename
        
    except Exception as e:
        print(f"    ✗ Could not create composite: {e}")
        return None

def export_geotiff(results, aoi, year, output_dir='outputs', region_name='Region'):
    """Export results as GeoTIFF for GIS analysis."""
    
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"\nExporting GeoTIFF data...")
    
    # Export soil loss
    loss_file = os.path.join(output_dir, f'MMF_SoilLoss_{region_name}_{year}.tif')
    print(f"  → Exporting: {loss_file}")
    
    geemap.ee_export_image(
        results['soil_loss'],
        filename=loss_file,
        scale=30,
        region=aoi,
        file_per_band=False
    )
    
    # Export runoff
    runoff_file = os.path.join(output_dir, f'MMF_Runoff_{region_name}_{year}.tif')
    print(f"  → Exporting: {runoff_file}")
    
    geemap.ee_export_image(
        results['runoff'],
        filename=runoff_file,
        scale=30,
        region=aoi,
        file_per_band=False
    )
    
    print("  ✓ GeoTIFF export complete")
    
    return {
        'soil_loss': loss_file,
        'runoff': runoff_file
    }

def main():
    """Main execution with validation examples."""
    
    # Define your regions and years
    regions = {
        'Thaba-Bosiu': ee.Geometry.Point([27.67, -29.35]).buffer(5000).bounds(),
        # Add more regions as needed:
        # 'Maseru': ee.Geometry.Point([27.48, -29.31]).buffer(5000).bounds(),
        # 'Roma': ee.Geometry.Point([27.72, -29.45]).buffer(5000).bounds(),
    }
    
    years = [2023]  # Add more years: [2021, 2022, 2023, 2024]
    
    # Output directory
    output_dir = 'MMF_Results'
    
    # Example validation data (replace with actual measured values)
    validation = {
        'measured_loss': 12.5,  # Mg/ha/yr from field plots
        'rusle_loss': 15.8,     # From your RUSLE model
        'location': 'Thaba-Bosiu',
        'measurement_method': 'Erosion pins / sediment traps',
        'notes': 'Measured in grassland plots, 2023 season'
    }
    
    # Process each region and year
    all_results = {}
    
    for region_name, aoi in regions.items():
        all_results[region_name] = {}
        
        for year in years:
            print(f"\n{'='*70}")
            print(f"Processing: {region_name} - {year}")
            print(f"{'='*70}")
            
            # Run MMF model
            results = run_daily_mmf(aoi, year, validation)
            
            # Visualize and save interactive map
            Map = visualize_results(aoi, results, year, output_dir)
            
            # Export map images (PNG)
            map_images = export_map_images(
                aoi, 
                results, 
                year, 
                output_dir, 
                region_name
            )
            
            # Export GeoTIFF for GIS
            geotiff_files = export_geotiff(
                results, 
                aoi, 
                year, 
                output_dir, 
                region_name
            )
            
            # Store results
            all_results[region_name][year] = {
                'stats': results['stats'],
                'maps': map_images,
                'geotiffs': geotiff_files
            }
    
    # Create summary report
    print(f"\n{'='*70}")
    print("PROCESSING COMPLETE - SUMMARY")
    print(f"{'='*70}")
    
    for region_name, years_data in all_results.items():
        print(f"\n{region_name}:")
        for year, data in years_data.items():
            mean_loss = data['stats'].get('MMF_Monthly_Aggregated_mean', 0)
            print(f"  {year}: {mean_loss:.2f} Mg/ha/yr")
    
    print(f"\nAll outputs saved to: {output_dir}/")
    print("\nFiles generated per region/year:")
    print("  - MMF_SoilLoss_<region>_<year>.png")
    print("  - MMF_Runoff_<region>_<year>.png")
    print("  - MMF_NDVI_<region>_<year>.png")
    print("  - MMF_Composite_<region>_<year>.png (combined view)")
    print("  - MMF_Map_<year>.html (interactive map)")
    print("  - MMF_SoilLoss_<region>_<year>.tif (GeoTIFF)")
    print("  - MMF_Runoff_<region>_<year>.tif (GeoTIFF)")
    
    print(f"\n{'='*70}")
    print("RECOMMENDATIONS FOR IMPROVEMENT")
    print(f"{'='*70}")
    print("1. Collect field data:")
    print("   - Install erosion pins at 3-5 representative sites")
    print("   - Measure runoff with simple collection troughs")
    print("   - Record vegetation height/cover monthly")
    print("\n2. Calibrate key parameters:")
    print("   - K-factor: Adjust based on soil texture analysis")
    print("   - Intensity: Use rain gauge data if available")
    print("   - Vegetation: Validate CC/GC with field photos")
    print("\n3. Temporal refinement:")
    print("   - Process actual daily rainfall events (not monthly)")
    print("   - Track soil moisture between storms")
    print("   - Account for seasonal vegetation changes")
    print("\n4. Compare with RUSLE:")
    print("   - MMF should be 0.5-1.5x RUSLE values")
    print("   - If ratio is too low: increase K-factor or intensity")
    print("   - If ratio is too high: check vegetation parameters")

if __name__ == "__main__":
    main()