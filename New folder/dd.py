import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
from torch.optim.lr_scheduler import OneCycleLR
from PIL import Image
import matplotlib.pyplot as plt
from tqdm import tqdm
import torchvision.models as models

FACTOR_NAMES = ['R', 'K', 'LS', 'C', 'P', 'soil_loss']
FACTOR_PALETTES = {
    'R': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
    'K': ["#ffeda0", "#feb24c", "#fc4e2a", "#bd0026", "#800026"],
    'LS': ["#edf8fb", "#b3cde3", "#8c96c6", "#8856a7", "#810f7c"],
    'C': ["#006837", "#31a354", "#78c679", "#c2e699", "#ffffcc"],
    'P': ["#08519c", "#3182bd", "#6baed6", "#bdd7e7", "#eff3ff"],
    'soil_loss': ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]
}


class RUSLEDataset(Dataset):
    def __init__(self, root_dir, transform=None):
        self.root_dir = root_dir
        self.transform = transform
        
        # Gather all available data entries
        self.data_entries = []
        
        # Find all unique regions
        self.regions = set()
        
        # Walk through the directory structure
        for region_name in os.listdir(root_dir):
            region_path = os.path.join(root_dir, region_name)
            if not os.path.isdir(region_path):
                continue
                
            self.regions.add(region_name)
            
            # Process each year for this region
            for year_folder in os.listdir(region_path):
                try:
                    year = int(year_folder)
                    year_path = os.path.join(region_path, year_folder)
                    
                    # Verify all factors exist for this year/region
                    has_all_factors = True
                    for factor in FACTOR_NAMES:
                        # Updated to match your naming convention
                        factor_file = f"{year}_{factor}.tif"
                        if not os.path.exists(os.path.join(year_path, factor_file)):
                            has_all_factors = False
                            print(f"Missing {factor_file} in {year_path}")
                            break
                    
                    if has_all_factors:
                        self.data_entries.append((year, region_name))
                except ValueError:
                    # Skip non-year folders
                    continue
        
        # Convert regions to a sorted list and create a mapping
        self.regions = sorted(list(self.regions))
        self.region_to_idx = {region: i for i, region in enumerate(self.regions)}
        
        print(f"Found {len(self.data_entries)} entries across {len(self.regions)} regions")

    def __len__(self):
        return len(self.data_entries)

    def __getitem__(self, idx):
        year, region = self.data_entries[idx]
        images = []

        for factor in FACTOR_NAMES:
            # Updated to match your naming convention
            factor_path = os.path.join(self.root_dir, region, str(year), f"{year}_{factor}.tif")
            # Open with PIL and convert to RGB
            image = Image.open(factor_path).convert('RGB')
            # Convert to PyTorch tensor
            image = transforms.ToTensor()(image)

            if self.transform:
                image = self.transform(image)

            images.append(image)

        # Normalize year and encode region as one-hot
        year_normalized = (year - 2009) / 14
        region_idx = self.region_to_idx[region]
        region_encoded = torch.zeros(len(self.regions))
        region_encoded[region_idx] = 1.0
        
        return torch.tensor(year_normalized, dtype=torch.float32), region_encoded, torch.stack(images)
# Model Architecture
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

# Perceptual Loss Using Pre-trained VGG
class PerceptualLoss(nn.Module):
    def __init__(self):
        super().__init__()
        vgg = models.vgg16(weights=models.VGG16_Weights.IMAGENET1K_V1).features[:9].eval()
        for param in vgg.parameters():
            param.requires_grad = False
        self.vgg = vgg

    def forward(self, generated, target):
        # Calculate perceptual loss for each factor separately and average
        perceptual_losses = []
        for i in range(generated.shape[1]):  # Iterate over factors
            perceptual_losses.append(F.l1_loss(self.vgg(generated[:, i]), self.vgg(target[:, i])))
        return torch.mean(torch.stack(perceptual_losses)) # Average the losses

# Training Function
def train_model(data_root, output_dir, num_epochs=1000, batch_size=16, lr=0.0001):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")

    transform = transforms.Compose([
        transforms.Resize((64, 64)),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    ])

    dataset = RUSLEDataset(data_root, transform=transform)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    
    # Create encoder with knowledge of how many regions exist
    encoder = YearRegionEncoder(num_regions=len(dataset.regions), latent_dim=256).to(device)
    generator = RUSLEGenerator().to(device)

    optimizer = torch.optim.Adam(
        list(encoder.parameters()) + list(generator.parameters()), lr=lr, betas=(0.5, 0.999)
    )
    scheduler = OneCycleLR(optimizer, max_lr=lr, total_steps=num_epochs * len(dataloader))

    perceptual_loss = PerceptualLoss().to(device)
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Save the region mapping for later use
    region_mapping = {
        'regions': dataset.regions,
        'region_to_idx': dataset.region_to_idx,
        'idx_to_region': {idx: region for region, idx in dataset.region_to_idx.items()}
    }
    torch.save(region_mapping, os.path.join(output_dir, 'region_mapping.pth'))

    for epoch in range(num_epochs):
        encoder.train()
        generator.train()
        total_loss = 0
        progress_bar = tqdm(dataloader, desc=f'Epoch {epoch+1}/{num_epochs}')

        for years, regions, real_images in progress_bar:
            years, regions, real_images = years.to(device), regions.to(device), real_images.to(device)
            latent = encoder(years, regions)
            generated_images = generator(latent)

            recon_loss = F.smooth_l1_loss(generated_images, real_images)
            percept_loss = perceptual_loss(generated_images, real_images)
            loss = recon_loss + 0.1 * percept_loss

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(generator.parameters(), 1.0)
            optimizer.step()
            scheduler.step()

            total_loss += loss.item()
            progress_bar.set_postfix({'loss': loss.item()})

        if (epoch + 1) % 100 == 0:
            checkpoint_path = os.path.join(output_dir, f'rusle_checkpoint_{epoch+1}.pth')
            torch.save({
                'encoder': encoder.state_dict(),
                'generator': generator.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch
            }, checkpoint_path)

    return encoder, generator, region_mapping

# Generate Future Images
def generate_rusle_factors(year, region, encoder, generator, device, output_base_dir, region_mapping=None):
    """
    Generate RUSLE factor images for a specific year and region
    
    Args:
        year: The year to generate predictions for
        region: The region identifier (string)
        encoder: Trained encoder model
        generator: Trained generator model
        device: Torch device
        output_base_dir: Base directory to save the generated images
        region_mapping: Optional dictionary containing region mapping information
    """
    encoder.eval()
    generator.eval()
    
    # If region_mapping is not provided but needed, try to load it
    if region_mapping is None and not isinstance(region, torch.Tensor):
        mapping_path = os.path.join(output_base_dir, 'region_mapping.pth')
        if os.path.exists(mapping_path):
            region_mapping = torch.load(mapping_path)
        else:
            raise ValueError("Region mapping not provided or found")
    
    # Get region name and create corresponding one-hot encoding
    if isinstance(region, torch.Tensor):
        # Already a tensor (one-hot encoded)
        region_encoded = region.to(device)
        if region_mapping:
            idx = torch.argmax(region).item()
            region_name = region_mapping['idx_to_region'][idx]
        else:
            region_name = f"region_{torch.argmax(region).item()}"
    else:
        # String identifier
        if region_mapping is None:
            raise ValueError("region_mapping required when region is a string")
        
        if region not in region_mapping['region_to_idx']:
            raise ValueError(f"Unknown region: {region}. Available regions: {region_mapping['regions']}")
        
        region_idx = region_mapping['region_to_idx'][region]
        region_encoded = torch.zeros(len(region_mapping['regions']), device=device)
        region_encoded[region_idx] = 1.0
        region_name = region
    
    # Create output directories following your structure
    region_output_dir = os.path.join(output_base_dir, region_name)
    os.makedirs(region_output_dir, exist_ok=True)
    
    year_output_dir = os.path.join(region_output_dir, str(year))
    os.makedirs(year_output_dir, exist_ok=True)
    
    with torch.no_grad():
        # Process inputs
        year_normalized = torch.tensor([(year - 2009) / 14], dtype=torch.float32).to(device)
        
        # Generate images
        latent = encoder(year_normalized, region_encoded.unsqueeze(0))
        generated = generator(latent)
        
        # Denormalize images from [-1, 1] to [0, 1]
        generated = (generated + 1) / 2
        generated = generated.cpu()
        
        # Save each factor as a separate image
        for i, (factor, img) in enumerate(zip(FACTOR_NAMES, generated[0])):
            # Convert to PIL Image and save
            img_pil = transforms.ToPILImage()(img)
            img_path = os.path.join(year_output_dir, f"{year}_{factor}.tif")  # Updated format
            img_pil.save(img_path)
            
        # Optionally create and save a visualization figure
        fig, axes = plt.subplots(2, 3, figsize=(15, 10))
        axes = axes.ravel()
        
        for i, (factor, img) in enumerate(zip(FACTOR_NAMES, generated[0])):
            axes[i].imshow(img.permute(1, 2, 0))
            axes[i].set_title(f"{factor}")
            axes[i].axis('off')
            
        plt.suptitle(f"Region: {region_name}, Year: {year}")
        plt.tight_layout()
        fig_path = os.path.join(year_output_dir, "visualization.png")
        plt.savefig(fig_path)
        plt.close(fig)
        
        return year_output_dir

# Function to load models and generate predictions
def generate_predictions(checkpoint_path, output_base_dir, regions, years, device=None):
    """
    Load trained models and generate predictions for specified regions and years
    
    Args:
        checkpoint_path: Path to the trained model checkpoint
        output_base_dir: Base directory to save generated images
        regions: List of region names to generate predictions for
        years: List of years to generate predictions for
        device: Torch device to use (will use CUDA if available when None)
    """
    if device is None:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
    # Load region mapping
    mapping_path = os.path.join(os.path.dirname(checkpoint_path), 'region_mapping.pth')
    if not os.path.exists(mapping_path):
        raise ValueError(f"Region mapping file not found at {mapping_path}")
        
    region_mapping = torch.load(mapping_path)
    
    # Load model checkpoint
    checkpoint = torch.load(checkpoint_path, map_location=device)
    
    # Initialize models
    encoder = YearRegionEncoder(num_regions=len(region_mapping['regions']), latent_dim=256).to(device)
    generator = RUSLEGenerator().to(device)
    
    # Load state dicts
    encoder.load_state_dict(checkpoint['encoder'])
    generator.load_state_dict(checkpoint['generator'])
    
    print(f"Loaded models from checkpoint {checkpoint_path}")
    print(f"Available regions: {region_mapping['regions']}")
    
    # Generate predictions
    for region in regions:
        if region not in region_mapping['region_to_idx']:
            print(f"Warning: Region '{region}' not found in available regions. Skipping.")
            continue
            
        for year in years:
            output_path = generate_rusle_factors(
                year,
                region,
                encoder,
                generator,
                device,
                output_base_dir,
                region_mapping
            )
            print(f"Generated predictions for {region} in {year} at {output_path}")

# Example usage
def main():
    DATA_DIR = 'RUSLE_Outputs'
    OUTPUT_DIR = 'RUSLE_Outputs'
    
    # Train the model
    encoder, generator, region_mapping= train_model(DATA_DIR, OUTPUT_DIR)
    
    # Generate predictions for future years for all regions
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    future_years = [2025, 2026, 2027, 2028, 2029, 2030]
    
    for region in region_mapping['regions']:
        for year in future_years:
            generate_rusle_factors(
                year, 
                region, 
                encoder, 
                generator, 
                device, 
                OUTPUT_DIR,
                region_mapping
            )
            print(f"Generated predictions for {region} in {year}")

if __name__ == "__main__":
    main()