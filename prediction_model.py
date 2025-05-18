import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import matplotlib.pyplot as plt
from torchvision import models

# Constants from your original code
FACTOR_NAMES = ['R', 'K', 'LS', 'C', 'P', 'soil_loss']
FACTOR_PALETTES = {
    'R': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
    'K': ["#ffeda0", "#feb24c", "#fc4e2a", "#bd0026", "#800026"],
    'LS': ["#edf8fb", "#b3cde3", "#8c96c6", "#8856a7", "#810f7c"],
    'C': ["#006837", "#31a354", "#78c679", "#c2e699", "#ffffcc"],
    'P': ["#08519c", "#3182bd", "#6baed6", "#bdd7e7", "#eff3ff"],
    'soil_loss': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]
}

# Model Architecture (same as original)
class YearRegionEncoder(nn.Module):
    def __init__(self, num_regions, latent_dim=256):
        super().__init__()
        
        # Process year and region separately then combine
        self.year_encoder = nn.Sequential(
            nn.Linear(1, 64),
            nn.LeakyReLU(0.2),
            nn.Linear(64, 128),
            nn.LeakyReLU(0.2)
        )
        
        self.region_encoder = nn.Sequential(
            nn.Linear(num_regions, 64),
            nn.LeakyReLU(0.2),
            nn.Linear(64, 128),
            nn.LeakyReLU(0.2)
        )
        
        # Combine and produce final embedding
        self.combiner = nn.Sequential(
            nn.Linear(256, latent_dim),
            nn.Tanh()
        )

    def forward(self, year, region):
        year_feat = self.year_encoder(year.unsqueeze(1))
        region_feat = self.region_encoder(region)
        combined = torch.cat([year_feat, region_feat], dim=1)
        return self.combiner(combined)

class RUSLEGenerator(nn.Module):
    def __init__(self, latent_dim=256, num_channels=3):
        super().__init__()

        self.initial_size = 4
        self.fc = nn.Linear(latent_dim, 512 * self.initial_size * self.initial_size)

        self.conv_blocks = nn.ModuleList([
            nn.Sequential(
                nn.utils.spectral_norm(nn.ConvTranspose2d(512, 256, 4, 2, 1)),
                nn.BatchNorm2d(256),
                nn.LeakyReLU(0.2),
                nn.Dropout2d(0.3)
            ),
            nn.Sequential(
                nn.utils.spectral_norm(nn.ConvTranspose2d(256, 128, 4, 2, 1)),
                nn.BatchNorm2d(128),
                nn.LeakyReLU(0.2),
                nn.Dropout2d(0.3)
            ),
            nn.Sequential(
                nn.utils.spectral_norm(nn.ConvTranspose2d(128, 64, 4, 2, 1)),
                nn.BatchNorm2d(64),
                nn.LeakyReLU(0.2)
            ),
            nn.Sequential(
                nn.utils.spectral_norm(nn.ConvTranspose2d(64, 32, 4, 2, 1)),
                nn.BatchNorm2d(32),
                nn.LeakyReLU(0.2)
            )
        ])

        self.factor_heads = nn.ModuleList([
            nn.Conv2d(32, num_channels, 3, 1, 1)
            for _ in FACTOR_NAMES
        ])

    def forward(self, z):
        x = self.fc(z)
        x = x.view(-1, 512, self.initial_size, self.initial_size)

        for conv_block in self.conv_blocks:
            x = conv_block(x)

        return torch.stack([torch.tanh(head(x)) for head in self.factor_heads], dim=1)

# Load model for Flask app - using the checkpoint regions approach
def load_trained_model(checkpoint_path='New folder/RUSLE_Outputs_future/rusle_checkpoint_1500.pth', data_dir='New folder/RUSLE_Outputs'):
    """
    Load a trained RUSLE model from a checkpoint, handling region mismatches.
    
    Args:
        checkpoint_path: Path to the model checkpoint
        data_dir: Directory containing the RUSLE data
        
    Returns:
        tuple: (encoder, generator, region_to_idx) - The loaded models and region mapping
    """
    device = torch.device('cpu')  # For web app, typically use CPU unless GPU is configured
    
    print(f"Loading model checkpoint from: {checkpoint_path}")
    
    # First load the checkpoint to inspect its shape
    try:
        checkpoint = torch.load(checkpoint_path, map_location=device)
        print("Checkpoint loaded successfully")
    except Exception as e:
        print(f"Error loading checkpoint: {e}")
        raise RuntimeError(f"Failed to load model checkpoint: {str(e)}")
    
    # Extract the number of regions from the checkpoint
    try:
        region_encoder_shape = checkpoint['encoder']['region_encoder.0.weight'].shape
        num_regions_in_checkpoint = region_encoder_shape[1]
        print(f"Detected {num_regions_in_checkpoint} regions in checkpoint")
    except KeyError as e:
        print(f"Error accessing model structure: {e}")
        raise RuntimeError(f"Checkpoint has unexpected structure: {str(e)}")
    
    # Try to load the region mapping if it exists in the same directory as the checkpoint
    checkpoint_dir = os.path.dirname(checkpoint_path)
    mapping_path = os.path.join(checkpoint_dir, 'region_mapping.pth')
    
    if os.path.exists(mapping_path):
        # Load the original region mapping
        try:
            region_mapping = torch.load(mapping_path)
            regions = [region_mapping[i] for i in range(num_regions_in_checkpoint)]
            print(f"Loaded regions from mapping: {regions}")
        except Exception as e:
            print(f"Error loading region mapping: {e}")
            # Fall back to generic region names
            regions = [f"region_{i}" for i in range(num_regions_in_checkpoint)]
            print(f"Created generic region names: {regions}")
    else:
        # Create generic region names
        regions = [f"region_{i}" for i in range(num_regions_in_checkpoint)]
        print(f"No region mapping found. Created generic region names: {regions}")
    
    # Create region_to_idx mapping
    region_to_idx = {region: i for i, region in enumerate(regions)}
    
    # Initialize models with the correct number of regions
    try:
        encoder = YearRegionEncoder(num_regions=num_regions_in_checkpoint).to(device)
        generator = RUSLEGenerator().to(device)
    except Exception as e:
        print(f"Error initializing models: {e}")
        raise RuntimeError(f"Failed to initialize models: {str(e)}")

    # Load checkpoint
    try:
        encoder.load_state_dict(checkpoint['encoder'])
        generator.load_state_dict(checkpoint['generator'])
    except Exception as e:
        print(f"Error loading model weights: {e}")
        # Try non-strict loading as a fallback
        try:
            print("Attempting non-strict weight loading...")
            encoder.load_state_dict(checkpoint['encoder'], strict=False)
            generator.load_state_dict(checkpoint['generator'], strict=False)
            print("Non-strict loading successful")
        except Exception as e2:
            raise RuntimeError(f"Failed to load model weights: {str(e2)}")

    # Set models to evaluation mode
    encoder.eval()
    generator.eval()
    
    print("Model loaded successfully")
    return encoder, generator, region_to_idx

# Generate RUSLE factors
def generate_rusle_factors(year, region, encoder, generator, device, output_dir, region_to_idx=None):
    """
    Generate RUSLE factor images for a specific year and region
    
    Args:
        year: The year to generate predictions for
        region: The region identifier (string)
        encoder: Trained encoder model
        generator: Trained generator model
        device: Torch device
        output_dir: Directory to save the generated images
        region_to_idx: Optional mapping from region names to indices
        
    Returns:
        str: Path to the directory containing the generated images
    """
    encoder.eval()
    generator.eval()
    
    print(f"Generating predictions for year {year} and region '{region}'")
    
    # Create output directories
    os.makedirs(output_dir, exist_ok=True)
    
    # Handle region input
    if isinstance(region, torch.Tensor):
        # Already a tensor (one-hot encoded)
        region_encoded = region.to(device)
        region_name = f"region_{torch.argmax(region).item()}"
    else:
        # String identifier
        if region_to_idx is None:
            mapping_path = os.path.join(os.path.dirname(output_dir), 'region_mapping.pth')
            if os.path.exists(mapping_path):
                # Load inverse mapping
                idx_to_region = torch.load(mapping_path)
                region_to_idx = {v: k for k, v in idx_to_region.items()}
                print(f"Loaded region mapping: {region_to_idx}")
            else:
                print("No region mapping found and none provided")
                raise ValueError("Region mapping is required but not provided")
        
        # Check if the region exists in our mapping
        if region not in region_to_idx:
            print(f"WARNING: Region '{region}' not found in mapping.")
            print(f"Available regions: {list(region_to_idx.keys())}")
            
            # Try case-insensitive matching as fallback
            region_lower = region.lower()
            matching_regions = [r for r in region_to_idx.keys() if r.lower() == region_lower]
            
            if matching_regions:
                region = matching_regions[0]  # Use the first match
                print(f"Using '{region}' as a case-insensitive match")
            else:
                # If we have a numeric region code, try to match numerically
                try:
                    region_code = int(region)
                    if region_code < len(region_to_idx):
                        # Get the region name at that index
                        region = list(region_to_idx.keys())[region_code]
                        print(f"Using region at index {region_code}: '{region}'")
                    else:
                        raise ValueError(f"Region code {region_code} out of range")
                except ValueError:
                    # If all fallbacks fail, raise an error
                    raise ValueError(f"Region '{region}' not found in mapping and no fallback matches")
            
        # Now encode the region
        region_idx = region_to_idx[region]
        region_encoded = torch.zeros(len(region_to_idx), device=device)
        region_encoded[region_idx] = 1.0
        region_name = region
    
    # Process the input based on type
    try:
        year_int = int(year)  # Convert to int if it's a string
        year_normalized = torch.tensor([(year_int - 2009) / 14], dtype=torch.float32).to(device)
        print(f"Normalized year: {year_normalized.item()}")
    except ValueError as e:
        raise ValueError(f"Invalid year format: {str(e)}")
    
    with torch.no_grad():
        try:
            # Generate images
            latent = encoder(year_normalized, region_encoded.unsqueeze(0))
            print(f"Generated latent vector with shape {latent.shape}")
            
            generated = generator(latent)
            print(f"Generated images with shape {generated.shape}")
            
            # Denormalize images from [-1, 1] to [0, 1]
            generated = (generated + 1) / 2
            generated = generated.cpu()
            
            # Save each factor as a separate image
            for i, (factor, img) in enumerate(zip(FACTOR_NAMES, generated[0])):
                try:
                    # Convert to PIL Image and save
                    img_pil = transforms.ToPILImage()(img)
                    img_path = os.path.join(output_dir, f"{year}_{factor}.tif")
                    img_pil.save(img_path)
                    print(f"Saved {factor} image to {img_path}")
                except Exception as e:
                    print(f"Error saving {factor} image: {e}")
                    # Continue with other factors even if one fails
            
            # Create and save a visualization figure
            try:
                fig, axes = plt.subplots(2, 3, figsize=(15, 10))
                axes = axes.ravel()
                
                for i, (factor, img) in enumerate(zip(FACTOR_NAMES, generated[0])):
                    axes[i].imshow(img.permute(1, 2, 0))
                    axes[i].set_title(f"{factor} - {year} - {region_name}")
                    axes[i].axis('off')
                    
                plt.tight_layout()
                fig_path = os.path.join(output_dir, f"{year}_visualization.png")
                plt.savefig(fig_path)
                plt.close(fig)
                print(f"Saved visualization to {fig_path}")
            except Exception as e:
                print(f"Error creating visualization: {e}")
                # This is non-critical, so continue
            
            return output_dir
            
        except Exception as e:
            print(f"Error in generation process: {e}")
            import traceback
            print(traceback.format_exc())
            raise RuntimeError(f"Failed to generate predictions: {str(e)}")

# Utility function to convert TIF to JPG for web display
def convert_image_to_jpg(tif_path):
    """
    Convert a TIF image to JPG for web display
    
    Args:
        tif_path: Path to the TIF image
        
    Returns:
        str: Path to the converted JPG image
    """
    try:
        # Get the directory and filename
        directory = os.path.dirname(tif_path)
        filename = os.path.basename(tif_path)
        name, _ = os.path.splitext(filename)
        
        # Create the output path
        jpg_path = os.path.join(directory, f"{name}.jpg")
        
        # Open the image and convert
        img = Image.open(tif_path)
        
        # If the image has more than 3 channels, convert to RGB
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        # Save as JPG
        img.save(jpg_path, 'JPEG', quality=90)
        
        # Return the relative path for web use
        rel_path = jpg_path.replace('static/', '/')
        if not rel_path.startswith('/'):
            rel_path = '/' + rel_path
            
        return rel_path
        
    except Exception as e:
        print(f"Error converting {tif_path} to JPG: {e}")
        # Return the original path if conversion fails
        return tif_path.replace('static/', '/')