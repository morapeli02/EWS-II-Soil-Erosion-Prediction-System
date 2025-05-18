import torch
import matplotlib.pyplot as plt
from torchvision import transforms
from prediction_model import RUSLEDataset, YearRegionEncoder, RUSLEGenerator, PerceptualLoss, FACTOR_NAMES
import os
from skimage.metrics import structural_similarity as ssim_score # type: ignore
from skimage.metrics import peak_signal_noise_ratio as psnr_score # type: ignore
import numpy as np




def evaluate_test_set(encoder, generator, test_loader, device):
    encoder.eval()
    generator.eval()

    total_mae = 0
    total_ssim = 0
    total_psnr = 0
    count = 0

    for years, regions, real_images in test_loader:
        years, regions, real_images = years.to(device), regions.to(device), real_images.to(device)
        with torch.no_grad():
            latent = encoder(years, regions)
            preds = generator(latent)
            preds = (preds + 1) / 2  # Denormalize to [0, 1]
            real_images = (real_images + 1) / 2  # Same here

        for i in range(preds.size(0)):
            for f in range(preds.size(1)):  # For each factor
                pred_img = preds[i, f].cpu().numpy().transpose(1, 2, 0)
                real_img = real_images[i, f].cpu().numpy().transpose(1, 2, 0)

                pred_gray = np.mean(pred_img, axis=2)
                real_gray = np.mean(real_img, axis=2)

                # MAE
                mae = np.mean(np.abs(pred_gray - real_gray))
                total_mae += mae

                # SSIM
                ssim = ssim_score(real_gray, pred_gray, data_range=1.0)
                total_ssim += ssim

                # PSNR
                psnr = psnr_score(real_gray, pred_gray, data_range=1.0)
                total_psnr += psnr

                count += 1

    avg_mae = total_mae / count
    mae_similarity = 100 * (1 - avg_mae)  # Assuming pixel range [0, 1]

    avg_ssim = total_ssim / count
    avg_psnr = total_psnr / count

    print("\n🔍 Quantitative Evaluation on Test Set:")
    print(f"🟢 MAE Similarity     : {mae_similarity:.2f}%")
    print(f"🟢 SSIM (Structure)   : {avg_ssim:.2f} ({avg_ssim*100:.2f}%)")
    print(f"🟢 PSNR (Fidelity)    : {avg_psnr:.2f} dB")


# ---- Load Best Model ----
def load_best_model(model_path, num_regions):
    encoder = YearRegionEncoder(num_regions=num_regions)
    generator = RUSLEGenerator()
    checkpoint = torch.load(model_path, map_location='cpu')
    encoder.load_state_dict(checkpoint['encoder'])
    generator.load_state_dict(checkpoint['generator'])
    return encoder.eval(), generator.eval()

# ---- Visualization Utility ----
def visualize_predictions(encoder, generator, test_loader, device, region_to_idx, num_samples=3):
    transform_to_pil = transforms.ToPILImage()
    samples = 0

    for years, regions, real_images in test_loader:
        years, regions, real_images = years.to(device), regions.to(device), real_images.to(device)
        latent = encoder(years, regions)
        preds = generator(latent)
        preds = (preds + 1) / 2  # Denormalize

        for b in range(min(num_samples, real_images.size(0))):
            fig, axes = plt.subplots(2, 6, figsize=(18, 6))
            for i in range(6):
                axes[0, i].imshow(transform_to_pil(real_images[b, i].cpu()))
                axes[0, i].set_title(f"GT: {FACTOR_NAMES[i]}")
                axes[0, i].axis('off')
                
                axes[1, i].imshow(transform_to_pil(preds[b, i].cpu()))
                axes[1, i].set_title(f"Pred: {FACTOR_NAMES[i]}")
                axes[1, i].axis('off')
            
            plt.tight_layout()
            plt.savefig(f"comparison_sample_{samples + 1}.png")
            plt.close()


            samples += 1
            if samples >= num_samples:
                return

# ---- Main Run ----
def compare_test_predictions():
    data_dir = 'New folder/RUSLE_Outputs'
    model_path = 'New folder/RUSLE_Outputs_future/rusle_checkpoint_1500.pth'
    batch_size = 1

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    dataset = RUSLEDataset(data_dir, transform=transforms.Compose([
        transforms.Resize((64, 64)),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    ]))

    # Split dataset
    from torch.utils.data import random_split, DataLoader
    total_size = len(dataset)
    val_size = int(0.15 * total_size)
    test_size = int(0.15 * total_size)
    train_size = total_size - val_size - test_size
    train_set, val_set, test_set = random_split(dataset, [train_size, val_size, test_size])
    test_loader = DataLoader(test_set, batch_size=batch_size, shuffle=True)

    encoder, generator = load_best_model(model_path, num_regions=len(dataset.regions))
    encoder.to(device)
    generator.to(device)

    visualize_predictions(encoder, generator, test_loader, device, dataset.region_to_idx, num_samples=3)
    print("\n🧪 Running accuracy evaluation on test set...")
    evaluate_test_set(encoder, generator, test_loader, device)


# 🔁 Run it
compare_test_predictions()
