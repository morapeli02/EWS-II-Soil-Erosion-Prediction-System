from flask import Flask, render_template, redirect, url_for, session, request, flash, send_file 
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_ckeditor import CKEditor
import bleach
from io import BytesIO
from PIL import Image
import base64
from docx import Document
from docx.shared import Inches
import os
import shutil
import uuid
import ee
import math
import torch
import re
import logging
import tempfile
from html import unescape

from Rusle_model import process_year, download_aoi_visualizations_gee
from prediction_model import generate_rusle_factors, load_trained_model


app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Initialize CKEditor
ckeditor = CKEditor(app)
app.config['CKEDITOR_PKG_TYPE'] = 'standard'

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///soil_erosion.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin' , expert or 'user'

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    author = db.Column(db.String(80), nullable=False)
    content = db.Column(db.Text, nullable=False)
    soil_loss_id = db.Column(db.Integer, db.ForeignKey('soil_erosion_estimates.id'), nullable=False)  # Foreign Key to soil_erosion_estimates
    
        
class News(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    
class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)

class AboutUs(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    
class AreaOfInterest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    region_name = db.Column(db.String(255), nullable=False)
    region_code = db.Column(db.Text, nullable=False)  # Stores the EE geometry polygon definition
    soil_erosion_estimates = db.relationship('soil_erosion_estimates', backref='area', lazy=True)
    
class soil_erosion_estimates(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    r_factor_stats = db.Column(db.String(200), nullable=True)
    k_factor_stats = db.Column(db.String(200), nullable=True)
    ls_factor_stats = db.Column(db.String(200), nullable=True)
    c_factor_stats = db.Column(db.String(200), nullable=True)
    p_factor_stats = db.Column(db.String(200), nullable=True)
    soil_loss_stats = db.Column(db.String(200), nullable=True)
    r_factor_image = db.Column(db.String(200), nullable=True)
    k_factor_image = db.Column(db.String(200), nullable=True)
    ls_factor_image = db.Column(db.String(200), nullable=True)
    c_factor_image = db.Column(db.String(200), nullable=True)
    p_factor_image = db.Column(db.String(200), nullable=True)
    soil_loss_image = db.Column(db.String(200), nullable=True)
    soil_loss_detailed_stats = db.Column(db.String(200), nullable=True)
    aoi_image=db.Column(db.String(200), nullable=True)
    area_of_interest_id = db.Column(db.Integer, db.ForeignKey('area_of_interest.id'), nullable=True)
    
    # Changed unique constraint to be a combination of year and area
    __table_args__ = (db.UniqueConstraint('year', 'area_of_interest_id', name='_year_area_uc'),)
# Add this after defining all your models


# Home route
@app.route('/')
def home():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    news_list = News.query.order_by(News.id.desc()).all()
    print(news_list)
    if role=='admin':
        return render_template('adhome.html', logged_in=logged_in, username=username, role=role, news_list=news_list)    
    else:
        return render_template('home.html', logged_in=logged_in, username=username, role=role, news_list=news_list)
# Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:
            session['logged_in'] = True
            session['username'] = username
            session['role'] = user.role
            flash('Login successful!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Invalid username or password.', 'danger')
    return render_template('login.html')
# Admin: Manage Users
@app.route('/manage_users', methods=['GET', 'POST'])
def manage_users():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    if session.get('role') != 'admin':
        flash('Unauthorized access!', 'danger')
        return redirect(url_for('home'))
    users = User.query.all()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            username = request.form.get('username')
            password = request.form.get('password')
            role = request.form.get('role')
            new_user = User(username=username, password=password, role=role)
            db.session.add(new_user)
            db.session.commit()
            flash('User added successfully!', 'success')
            return redirect(url_for('manage_users'))
        elif action == 'delete':
            user_id = request.form.get('user_id')
            User.query.filter_by(id=user_id).delete()
            db.session.commit()
            flash('User deleted successfully!', 'success')
            return redirect(url_for('manage_users'))
    return render_template('manage_users.html', logged_in=logged_in, username=username, role=role,users=users)

# Admin: Manage News
@app.route('/manage_news', methods=['GET', 'POST'])
def manage_news():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    if session.get('role') != 'admin':
        flash('Unauthorized access!', 'danger')
        return redirect(url_for('home'))
    news_list = News.query.all()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            title = request.form.get('title')
            content = request.form.get('content')
            new_news = News(title=title, content=content)
            db.session.add(new_news)
            db.session.commit()
            flash('News added successfully!', 'success')
            return redirect(url_for('manage_news'))
        elif action == 'delete':
            news_id = request.form.get('news_id')
            News.query.filter_by(id=news_id).delete()
            db.session.commit()
            flash('News deleted successfully!', 'success')
            return redirect(url_for('manage_news'))
    return render_template('manage_news.html',logged_in=logged_in, username=username, role=role, news_list=news_list)

# Admin: Manage Notifications
@app.route('/manage_notifications', methods=['GET', 'POST'])
def manage_notifications():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    if session.get('role') != 'admin':
        flash('Unauthorized access!', 'danger')
        return redirect(url_for('home'))
    notifications = Notification.query.all()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            content = request.form.get('content')
            sanitized_content = bleach.clean(content, tags=['p', 'font', 'strong', 'a', 'br'], attributes={'a': ['href'], 'font': ['size', 'color'], 'p': ['align']})
            new_notification = Notification(content=sanitized_content)
            db.session.add(new_notification)
            db.session.commit()
            flash('Notification added successfully!', 'success')
            return redirect(url_for('manage_notifications'))
        elif action == 'delete':
            notification_id = request.form.get('notification_id')
            Notification.query.filter_by(id=notification_id).delete()
            db.session.commit()
            flash('Notification deleted successfully!', 'success')
            return redirect(url_for('notifications'))
    return render_template('manage_notifications.html',logged_in=logged_in, username=username, role=role, notifications=notifications)

# Admin: Manage About Us
@app.route('/manage_about', methods=['GET', 'POST'])
def manage_about():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    if session.get('role') != 'admin':
        flash('Unauthorized access!', 'danger')
        return redirect(url_for('home'))
    
    about_us = AboutUs.query.first()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'save':
            content = request.form.get('content')
            if about_us:
                about_us.content = content
            else:
                about_us = AboutUs(content=content)
                db.session.add(about_us)
                db.session.commit()
                return redirect(url_for('manage_about'))
            flash('"About Us" updated successfully!', 'success')
        
        elif action == 'delete':
            if about_us:
                db.session.delete(about_us)
                db.session.commit()
                flash('"About Us" deleted successfully!', 'success')
                return redirect(url_for('manage_about'))
            else:
                flash('No content to delete!', 'warning')
    
    return render_template('manage_about.html',logged_in=logged_in, username=username, role=role, about_us=about_us)

# About Us (Regular Users)
@app.route('/about')
def about():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    about_us = AboutUs.query.first()
    return render_template('about.html', about_us=about_us,logged_in=logged_in, username=username, role=role)

# Logout route
@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))









import os
import shutil
import re
import logging
from html import unescape
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import threading
import time

# Create a mutex for file operations
file_lock = threading.Lock()
@app.route('/download_report/<int:report_id>')
def download_report(report_id):
    """
    Generate and download a report as a PDF document.
    
    Args:
        report_id (int): The ID of the report to download
        
    Returns:
        Flask response with the PDF file attachment
    """
    # Start timing for performance tracking
    start_time = time.time()
    
    def clean_text(text):
        """Clean text by removing/replacing special characters and formatting long words"""
        if not text:
            return ''
        # Handle common Unicode characters and HTML entities
        text = text.replace('\u2014', '-').replace('\u2013', '-') \
                   .replace('\u2018', "'").replace('\u2019', "'") \
                   .replace('\u201C', '"').replace('\u201D', '"') \
                   .replace('\u2026', '...').replace('&nbsp;', ' ')
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Unescape HTML entities
        text = unescape(text)
        # Add space to extremely long words (prevents rendering issues)
        text = re.sub(r'(\S{30,})', r'\1 ', text)
        return text.strip()

    def get_image(img_url, temp_dir, idx, base_dir=None):
        """
        Retrieve and save an image to the temp directory.
        
        Args:
            img_url (str): URL or path of the image
            temp_dir (str): Directory to save the image
            idx (int): Index for naming the image file
            base_dir (str, optional): Base directory to resolve relative paths
            
        Returns:
            str or None: Path to the saved image or None if failed
        """
        if not img_url:
            return None
            
        # Acquire lock for file operations
        with file_lock:
            img_path = os.path.join(temp_dir, f'image_{idx}.jpg')
            
            try:
                # Check if it's a URL
                if img_url.startswith(('http://', 'https://')):
                    response = requests.get(img_url, stream=True, timeout=10)
                    if response.status_code == 200:
                        with open(img_path, 'wb') as f:
                            for chunk in response.iter_content(1024):
                                f.write(chunk)
                        return img_path
                    else:
                        logging.warning(f"Failed to download image: {img_url} - Status: {response.status_code}")
                        return None
                
                # Handle stats paths - extract the related image path
                if img_url and '_stats' in img_url:
                    # Extract the actual image path from stats path
                    img_path_from_stats = img_url.replace('_stats', '')
                    
                    # Try different file extensions if needed
                    possible_exts = ['.png', '.jpg', '.jpeg']
                    
                    for ext in possible_exts:
                        # Strip existing extension and add new one
                        base_img_path = os.path.splitext(img_path_from_stats)[0]
                        test_path = f"{base_img_path}{ext}"
                        
                        if os.path.exists(test_path):
                            logging.info(f"Found stats image alternative at: {test_path}")
                            shutil.copy2(test_path, img_path)
                            return img_path
                
                # Handle relative paths - try multiple possible locations
                possible_paths = []
                
                # If base_dir is provided, try joining with base_dir first
                if base_dir:
                    possible_paths.append(os.path.join(base_dir, os.path.basename(img_url)))
                    possible_paths.append(os.path.join(base_dir, img_url))
                
                # Add paths checking for the exact structure shown in the error
                if 'RusleOutputs' in img_url:
                    possible_paths.append(img_url)
                    possible_paths.append(os.path.join('static', img_url))
                else:
                    # For filenames only, try to construct the full path
                    if '/' not in img_url and base_dir:
                        region_name = base_dir.split(os.path.sep)[-2]  # Extract region name from base_dir
                        year = base_dir.split(os.path.sep)[-1]         # Extract year from base_dir
                        factor_part = os.path.splitext(img_url)[0]     # Extract factor name without extension
                        
                        # Try constructing paths like static/images/RusleOutputs/Mphosong/2011/2011_R_stats.png
                        constructed_path = os.path.join('static', 'images', 'RusleOutputs', region_name, year, f"{year}_{factor_part}.png")
                        possible_paths.append(constructed_path)
                
                # Original path as is
                possible_paths.append(img_url)
                
                # Try with explicit static folder if not already an absolute path
                if not os.path.isabs(img_url):
                    possible_paths.append(os.path.join(app.static_folder, img_url))
                    possible_paths.append(os.path.join('static', img_url))
                
                # Check common subdirectories in static folder
                if not img_url.startswith('static/'):
                    possible_paths.append(os.path.join('static', 'images', img_url))
                    possible_paths.append(os.path.join('static', 'img', img_url))
                
                # Log all paths we're checking
                logging.debug(f"Checking image paths: {possible_paths}")
                
                # Try each path
                for path in possible_paths:
                    if os.path.exists(path) and os.path.isfile(path):
                        logging.info(f"Found image at: {path}")
                        shutil.copy2(path, img_path)
                        return img_path
                
                # If we can't find the image, try to extract image path from stats string
                # This is a fallback mechanism for the case shown in the screenshot
                if img_url and 'stats' in img_url.lower():
                    # Try to extract image path from the stats string (assuming it contains the path)
                    # For example, if img_url is "static/images/RusleOutputs/Mphosong/2011/2011_R_stats.png"
                    # Try to construct an image path like "static/images/RusleOutputs/Mphosong/2011/2011_R.png"
                    try:
                        base_name = os.path.basename(img_url)
                        if '_stats' in base_name:
                            image_name = base_name.replace('_stats', '')
                            dir_path = os.path.dirname(img_url)
                            image_path = os.path.join(dir_path, image_name)
                            
                            # Check if the constructed path exists
                            if os.path.exists(image_path) and os.path.isfile(image_path):
                                logging.info(f"Found image at: {image_path} (constructed from stats path)")
                                shutil.copy2(image_path, img_path)
                                return img_path
                    except Exception as e:
                        logging.error(f"Failed to extract image path from stats: {e}")
                
                logging.warning(f"Image not found at any path: {possible_paths}")
                return None
                
            except Exception as e:
                logging.error(f"Image processing failed: {img_url} - {str(e)}")
                return None

    try:
        # Fetch DB entries
        report = Report.query.get_or_404(report_id)
        erosion_data = soil_erosion_estimates.query.get(report.soil_loss_id)
        if not erosion_data:
            return "Erosion data not found", 404
            
        # Get the associated area of interest to access region name
        area_info = None
        if erosion_data.area_of_interest_id:
            area_info = AreaOfInterest.query.get(erosion_data.area_of_interest_id)
        
        # Get region name from area_info if available
        region_name = area_info.region_name if area_info else "Not Specified"
        
        # Construct base directory for image paths using region name
        # Example: static/images/RusleOutputs/RegionName/2024/
        base_dir = None
        if region_name != "Not Specified":
            base_dir = os.path.join('static', 'images', 'RusleOutputs', region_name, str(erosion_data.year))
            # Make sure base_dir exists
            if not os.path.exists(base_dir):
                logging.warning(f"Base directory does not exist: {base_dir}")
                # Try alternative format - some systems might use different naming conventions
                base_dir = os.path.join('static', 'images', 'RusleOutputs', region_name.replace(' ', '_'), str(erosion_data.year))
                if not os.path.exists(base_dir):
                    logging.warning(f"Alternative base directory also does not exist: {base_dir}")
                    base_dir = None

        # Collect images and their data - now including AOI image
        images = []
        
        # First, add the AOI image if available
        if erosion_data.aoi_image:
            images.append({
                "url": erosion_data.aoi_image, 
                "label": "Area of Interest", 
                "stats": ""
            })
            
        # Get region name and year for constructing image paths if needed
        region_part = region_name.replace(' ', '_')
        year_part = str(erosion_data.year)
        
        # Add RUSLE factor images
        factors = [
            ("r_factor_image", "R Factor - Rainfall Erosivity", erosion_data.r_factor_stats),
            ("k_factor_image", "K Factor - Soil Erodibility", erosion_data.k_factor_stats),
            ("ls_factor_image", "LS Factor - Slope Length & Steepness", erosion_data.ls_factor_stats),
            ("c_factor_image", "C Factor - Cover Management", erosion_data.c_factor_stats),
            ("p_factor_image", "P Factor - Support Practice", erosion_data.p_factor_stats),
            ("soil_loss_image", "Soil Loss Assessment", erosion_data.soil_loss_stats),
        ]
        
        for attr, label, stats in factors:
            img_url = getattr(erosion_data, attr, None)
            if img_url:
                # If the image URL doesn't seem to be a complete path, try to construct it
                if img_url and not img_url.startswith(('http://', 'https://', '/', 'static')):
                    # Extract the factor code from attribute name (r_factor_image -> R)
                    factor_code = attr[0].upper() if attr else ""
                    
                    # For soil_loss_image, use special handling
                    if attr == "soil_loss_image":
                        factor_code = "soil_loss"
                    
                    # Try to construct full path based on pattern from example
                    constructed_path = f"static/images/RusleOutputs/{region_part}/{year_part}/{year_part}_{factor_code}.png"
                    logging.info(f"Original image URL: {img_url}, constructed path: {constructed_path}")
                    img_url = constructed_path
                
                images.append({"url": img_url, "label": label, "stats": stats})

        # Create temporary directory for image processing
        temp_dir = tempfile.mkdtemp()
        
        # Process all images
        processed_images = []
        for idx, img in enumerate(images):
            img_path = get_image(img['url'], temp_dir, idx, base_dir)
            
            # For stats paths, attempt additional fallback approach directly from the stats string
            if not img_path and 'stats' in img.get('stats', ''):
                stats_text = img.get('stats', '')
                # Try to extract path from stats text if it contains a file path
                if 'static/images' in stats_text:
                    path_match = re.search(r'(static/images/\S+)', stats_text)
                    if path_match:
                        extracted_path = path_match.group(1)
                        # Try to get the actual image path from the stats path
                        if '_stats.png' in extracted_path:
                            actual_img_path = extracted_path.replace('_stats.png', '.png')
                            if os.path.exists(actual_img_path):
                                shutil.copy2(actual_img_path, os.path.join(temp_dir, f'image_{idx}.jpg'))
                                img_path = os.path.join(temp_dir, f'image_{idx}.jpg')
            
            processed_images.append({
                "path": img_path,
                "label": img['label'],
                "stats": clean_text(img.get('stats', '')),  # Clean the stats text
                "url": img['url']
            })

        # Create PDF document with enhanced formatting
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
        from reportlab.platypus.tableofcontents import TableOfContents
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        
        # Create a BytesIO object to store the PDF
        output = BytesIO()
        
        # Create the PDF document
        doc = SimpleDocTemplate(
            output, 
            pagesize=landscape(letter),
            title=f"Soil Erosion Report {report_id}",
            author=report.author
        )
        
        # Define styles
        styles = getSampleStyleSheet()
        
        # Create custom styles
        title_style = ParagraphStyle(
            'Title',
            parent=styles['Title'],
            alignment=TA_CENTER,
            fontSize=24,
            spaceAfter=24,
            fontName='Helvetica-Bold'
        )
        
        subtitle_style = ParagraphStyle(
            'Subtitle',
            parent=styles['Heading1'],
            alignment=TA_CENTER,
            fontSize=18,
            spaceAfter=12,
            fontName='Helvetica-Bold'
        )
        
        info_style = ParagraphStyle(
            'Info',
            parent=styles['Normal'],
            alignment=TA_CENTER,
            fontSize=12,
            spaceAfter=24
        )
        
        heading1_style = ParagraphStyle(
            'Heading1',
            parent=styles['Heading1'],
            fontSize=16,
            spaceAfter=12,
            fontName='Helvetica-Bold'
        )
        
        heading2_style = ParagraphStyle(
            'Heading2',
            parent=styles['Heading2'],
            fontSize=14,
            spaceAfter=10,
            fontName='Helvetica-Bold'
        )
        
        normal_style = ParagraphStyle(
            'Normal',
            parent=styles['Normal'],
            fontSize=12,
            spaceAfter=12,
            leading=14  # Improved line spacing
        )
        
        # Create style for statistics text
        stats_style = ParagraphStyle(
            'Statistics',
            parent=styles['Normal'],
            fontSize=10,
            fontName='Helvetica-Oblique',
            spaceAfter=6,
            textColor=colors.darkblue
        )
        
        # Create content list
        content = []
        
        # Add cover page
        content.append(Paragraph(f'Soil Erosion Analysis Report', title_style))
        content.append(Spacer(1, 0.2*inch))
        content.append(Paragraph(f'Region: {region_name}', subtitle_style))
        content.append(Paragraph(f'For Year: {erosion_data.year}', subtitle_style))
        content.append(Spacer(1, 0.5*inch))
        content.append(Paragraph(f'Generated by: {report.author} | Date: {report.date.strftime("%B %d, %Y")}', info_style))
        
        # Add page break after cover
        content.append(PageBreak())
        
        # Add table of contents heading
        content.append(Paragraph('Table of Contents', heading1_style))
        content.append(Spacer(1, 0.2*inch))
        
        # Build the TOC entries manually
        toc_entries = ["Executive Summary", "Analysis"]
        
        # Add Area of Interest entry if that image exists
        if any(img["label"] == "Area of Interest" and img["path"] for img in processed_images):
            toc_entries.append("Area of Interest")
            
        toc_entries.extend(["Soil Erosion Data", "Factor Analysis"])
        
        if erosion_data.soil_loss_detailed_stats:
            toc_entries.append("Detailed Soil Loss Statistics")
        
        toc_entries.append("Conclusion")
        
        # Create manual TOC
        for i, entry in enumerate(toc_entries):
            toc_text = f"{i+1}. {entry}"
            toc_para = Paragraph(toc_text, normal_style)
            content.append(toc_para)
        
        content.append(PageBreak())
        
        # Add executive summary
        content.append(Paragraph('Executive Summary', heading1_style))
        
        summary_text = f"This report presents soil erosion analysis for {region_name}. "
        summary_text += "The analysis uses the RUSLE model to estimate soil loss based on various factors "
        summary_text += "including rainfall erosivity, soil erodibility, slope factors, cover management, and support practices."
        
        content.append(Paragraph(summary_text, normal_style))
        
        # Add main analysis section
        content.append(Paragraph('Analysis', heading1_style))
        
        # Clean and format the main content
        clean_content = clean_text(report.content)
        
        # Split content into paragraphs for better formatting
        paragraphs = clean_content.split('\n\n')
        for para in paragraphs:
            if para.strip():
                content.append(Paragraph(para.strip(), normal_style))
        
        # Add Area of Interest section if image is available
        aoi_image = next((img for img in processed_images if img["label"] == "Area of Interest" and img["path"]), None)
        if aoi_image:
            content.append(Paragraph('Area of Interest', heading1_style))
            content.append(Paragraph("The map below shows the area analyzed in this report.", normal_style))
            
            # Add AOI image if path exists
            if aoi_image["path"] and os.path.exists(aoi_image["path"]):
                # Make the AOI image larger as it's the main overview map
                img = Image(aoi_image["path"], width=9*inch, height=5*inch, kind='proportional')
                img.hAlign = 'CENTER'
                content.append(img)
            else:
                content.append(Paragraph("[Area of Interest image unavailable]", normal_style))
        
        # Add soil erosion data section
        content.append(Paragraph('Soil Erosion Data', heading1_style))
        
        # Introduction paragraph for the data section
        content.append(Paragraph("The following visualizations represent the key factors in the RUSLE soil erosion model. Each factor contributes to the overall soil loss estimation.", normal_style))
        
        # Filter out the AOI image from the images to be displayed in the factor analysis section
        factor_images = [img for img in processed_images if img["label"] != "Area of Interest"]
        
        # Add factor analysis header
        content.append(Paragraph('Factor Analysis', heading1_style))
        
        # Create factor image tables (2 columns)
        for i in range(0, len(factor_images), 2):
            table_data = []
            row = []
            
            for j in range(2):
                if i + j < len(factor_images):
                    img_data = factor_images[i + j]
                    cell_content = []
                    
                    # Add factor heading
                    cell_content.append(Paragraph(img_data["label"], heading2_style))
                    
                    # Add image if available
                    if img_data["path"] and os.path.exists(img_data["path"]):
                        img = Image(img_data["path"], width=4*inch, height=3*inch, kind='proportional')
                        img.hAlign = 'CENTER'
                        cell_content.append(img)
                    else:
                        # Try alternate paths
                        factor_label = img_data["label"].split(' ')[0]  # Get R, K, LS, C, P, Soil
                        factor_code = factor_label
                        if factor_label == "Soil":
                            factor_code = "soil_loss"
                            
                        # Try to construct alternate paths
                        alt_paths = [
                            f"static/images/RusleOutputs/{region_part}/{year_part}/{year_part}_{factor_code}.png",
                            f"static/images/RusleOutputs/{region_part}/{year_part}/{factor_code}.png",
                            f"static/images/RusleOutputs/{region_part}/{year_part}/{year_part}_{factor_label}.png",
                        ]
                        
                        image_found = False
                        for alt_path in alt_paths:
                            if os.path.exists(alt_path):
                                img = Image(alt_path, width=4*inch, height=3*inch, kind='proportional')
                                img.hAlign = 'CENTER'
                                cell_content.append(img)
                                image_found = True
                                break
                                
                        if not image_found:
                            cell_content.append(Paragraph("[Image unavailable]", normal_style))
                    
                    # Add stats data differently - don't show the path
                    if img_data["stats"]:
                        # If stats looks like a path, don't show it, instead show a generic message
                        if 'static/images' in img_data["stats"] or '_stats.png' in img_data["stats"]:
                            stats_text = "Statistics: Data available in source system"
                        else: 
                            stats_text = f"Statistics: {img_data['stats']}"
                            
                        cell_content.append(Paragraph(stats_text, stats_style))
                    
                    row.append(cell_content)
                else:
                    # Empty cell for odd number of images
                    row.append([])
            
            table_data.append(row)
            
            # Create the table
            col_widths = [4.5*inch, 4.5*inch]
            table = Table(table_data, colWidths=col_widths)
            
            # Style the table
            table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('LEFTPADDING', (0, 0), (-1, -1), 10),
                ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ]))
            
            content.append(table)
            content.append(Spacer(1, 0.3*inch))
        
        # Add detailed statistics if available
        # if erosion_data.soil_loss_detailed_stats:
        #     content.append(PageBreak())
        #     content.append(Paragraph('Detailed Soil Loss Statistics', heading1_style))
            
        #     # Split detailed stats into paragraphs for better formatting
        #     detailed_stats = clean_text(erosion_data.soil_loss_detailed_stats)
        #     stats_paragraphs = detailed_stats.split('\n\n')
        #     for para in stats_paragraphs:
        #         if para.strip():
        #             content.append(Paragraph(para.strip(), normal_style))
        
        # Add conclusion
        content.append(Paragraph('Conclusion', heading1_style))
        conclusion_text = f"This report provides a comprehensive analysis of soil erosion in {region_name}. "
        conclusion_text += "The findings indicate areas of concern that may require implementation of soil conservation practices."
        content.append(Paragraph(conclusion_text, normal_style))
        
        # Build the PDF
        doc.build(
            content, 
            onFirstPage=lambda canvas, doc: canvas.drawCentredString(
                doc.width/2, 0.5*inch, f"Soil Erosion Report {report_id} | Page {canvas.getPageNumber()}"
            ),
            onLaterPages=lambda canvas, doc: canvas.drawCentredString(
                doc.width/2, 0.5*inch, f"Soil Erosion Report {report_id} | Page {canvas.getPageNumber()}"
            )
        )
        
        # Log performance metrics
        processing_time = time.time() - start_time
        logging.info(f"Report {report_id} generated in {processing_time:.2f} seconds")

        # Reset file pointer to beginning
        output.seek(0)
        
        # Add region name to filename if available
        region_slug = ""
        if region_name and region_name != "Not Specified":
            # Create a safe filename slug from region name
            region_slug = re.sub(r'[^\w\s-]', '', region_name.lower())
            region_slug = re.sub(r'[-\s]+', '-', region_slug).strip('-_')
            region_slug = f"_{region_slug}"
        
        return send_file(
            output,
            download_name=f"soil_erosion_report_{report_id}{region_slug}_{erosion_data.year}.pdf",
            as_attachment=True,
            mimetype='application/pdf'
        )

    except Exception as e:
        logging.exception(f"Report generation failed: {str(e)}")
        return f"Error generating report: {str(e)}", 500
        
    finally:
        # Clean up temporary files
        if 'temp_dir' in locals():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logging.error(f"Failed to clean up temp directory: {str(e)}")
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################
# Reports route
@app.route('/reports', methods=['GET', 'POST'])
def reports():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    is_admin_or_expert = role in ['admin', 'expert']
    
    # Get list of available years for the dropdown
    available_years = sorted(set([result.year for result in soil_erosion_estimates.query.all()]))
    
    selected_year = None
    if request.method == 'POST':
        selected_year = request.form.get('year')
    
    reports_data = []
    
    # Query reports based on selected year
    if selected_year:
        reports = Report.query.join(soil_erosion_estimates).filter(soil_erosion_estimates.year == int(selected_year)).all()
    else:
        reports = Report.query.order_by(Report.date.desc()).all()
    
    for report in reports:
        # Get erosion data for the corresponding soil loss estimate
        erosion_data = soil_erosion_estimates.query.get(report.soil_loss_id)
        
        if erosion_data:
            # Get region name from the linked area of interest
            region_name = "Unknown"
            if erosion_data.area_of_interest_id:
                area = AreaOfInterest.query.get(erosion_data.area_of_interest_id)
                if area:
                    region_name = area.region_name
            
            # Add the region_name to the erosion_data object (as a non-persistent attribute)
            erosion_data.region_name = region_name
            
            # Create images list similar to visualization route
            images = [
                {"label": "R Factor", "url": convert_image_to_jpg(erosion_data.r_factor_image), "stats_url": erosion_data.r_factor_stats},
                {"label": "K Factor", "url": convert_image_to_jpg(erosion_data.k_factor_image), "stats_url": erosion_data.k_factor_stats},
                {"label": "LS Factor", "url": convert_image_to_jpg(erosion_data.ls_factor_image), "stats_url": erosion_data.ls_factor_stats},
                {"label": "C Factor", "url": convert_image_to_jpg(erosion_data.c_factor_image), "stats_url": erosion_data.c_factor_stats},
                {"label": "P Factor", "url": convert_image_to_jpg(erosion_data.p_factor_image), "stats_url": erosion_data.p_factor_stats},
                {"label": "Soil Loss", "url": convert_image_to_jpg(erosion_data.soil_loss_image), "stats_url": erosion_data.soil_loss_stats},
                {"label": "Area of interest Image", "url":convert_image_to_jpg(erosion_data.aoi_image),"stats_url":None},
                {"label": "Detailed Soil Loss Stats","url":None, "stats_url": erosion_data.soil_loss_detailed_stats}
            ]
            
            # Filter out any None values
            images = [img for img in images if img['url'] is not None or img['stats_url'] is not None]
            reports_data.append((report, erosion_data, images))
        else:
            # Create a dummy object with a region_name attribute
            class DummyObject:
                pass
            dummy = DummyObject()
            dummy.region_name = "Unknown"
            reports_data.append((report, dummy, []))
    
    return render_template('reports.html', 
                          reports=reports_data,
                          logged_in=logged_in,
                          username=username,
                          role=role,
                          is_admin_or_expert=is_admin_or_expert,
                          available_years=available_years,
                          selected_year=selected_year)
##############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################
@app.route('/add_report', methods=['GET', 'POST']) 
def add_report():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)

    if not logged_in or role not in ['admin', 'expert']:
        flash('Only expert users can generate reports.', 'danger')
        return redirect(url_for('reports'))

    available_regions = db.session.query(
        soil_erosion_estimates.area_of_interest_id,
        AreaOfInterest.region_name
    ).join(
        AreaOfInterest,
        soil_erosion_estimates.area_of_interest_id == AreaOfInterest.id
    ).distinct().all()

    available_years = []
    selected_region = None
    region_name = None

    if request.method == 'POST':
        print(f"FORM DATA: {request.form}")

        # Step 1: Get Years
        if 'get_years' in request.form:
            selected_region = request.form.get('region')

            if not selected_region:
                flash('Please select a region first', 'warning')
                return redirect(url_for('add_report'))

            region_id = int(selected_region)
            available_years = db.session.query(soil_erosion_estimates.year).filter_by(
                area_of_interest_id=region_id
            ).distinct().all()

            available_years = [year[0] for year in available_years]
            region_name = db.session.query(AreaOfInterest.region_name).filter_by(id=region_id).scalar()

            return render_template(
                'add_report.html',
                logged_in=logged_in,
                username=username,
                role=role,
                available_regions=available_regions,
                available_years=available_years,
                selected_region=selected_region,
                region_name=region_name
            )

        # Step 2: Submit Report
        elif 'submit_report' in request.form:
            selected_region = request.form.get('region')
            selected_year = request.form.get('year')
            report_content = request.form.get('content')

            if not selected_region or not selected_year or not report_content:
                flash('Please fill in all required fields', 'danger')
                region_id = int(selected_region)
                available_years = [year[0] for year in db.session.query(
                    soil_erosion_estimates.year
                ).filter_by(area_of_interest_id=region_id).distinct().all()]
                region_name = db.session.query(AreaOfInterest.region_name).filter_by(id=region_id).scalar()

                return render_template(
                    'add_report.html',
                    logged_in=logged_in,
                    username=username,
                    role=role,
                    available_regions=available_regions,
                    available_years=available_years,
                    selected_region=selected_region,
                    region_name=region_name
                )

            try:
                soil_loss_estimate = soil_erosion_estimates.query.filter_by(
                    year=int(selected_year),
                    area_of_interest_id=int(selected_region)
                ).first()

                if not soil_loss_estimate:
                    flash('No soil loss estimate found for the selected criteria', 'danger')
                    return redirect(url_for('add_report'))

                new_report = Report(
                    soil_loss_id=soil_loss_estimate.id,
                    content=report_content,
                    author=username,
                    date=datetime.utcnow()
                )

                db.session.add(new_report)
                db.session.commit()

                flash('Report added successfully!', 'success')
                return redirect(url_for('reports'))

            except Exception as e:
                db.session.rollback()
                flash(f'Error adding report: {str(e)}', 'danger')
                return redirect(url_for('add_report'))

        else:
            flash('Invalid form submission', 'warning')
            return redirect(url_for('add_report'))

    return render_template(
        'add_report.html',
        logged_in=logged_in,
        username=username,
        role=role,
        available_regions=available_regions,
        available_years=available_years,
        selected_region=selected_region,
        region_name=region_name
    )
    
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################
    
# Visualization route
@app.route('/visualization', methods=['GET', 'POST'])
def visualization():
    logged_in = session.get('logged_in', False)
    username = session.get('username', None)
    role = session.get('role', None)
    
    import datetime  # Import datetime module properly
    
    images = []
    selected_year = None
    selected_area_id = None
    processing_message = None
    region_name = None
    model_mode = 'rusle'  # Default to RUSLE mode
    current_year = datetime.datetime.now().year
    

    # Get list of available areas for the dropdown
    areas = AreaOfInterest.query.all()

    if request.method == 'POST':
        # Get the model mode (RUSLE or Prediction)
        model_mode = request.form.get('model_mode', 'rusle')
        
        # Process based on model mode
        if model_mode == 'rusle':
            # Handle area of interest selection
            area_option = request.form.get('area_option')
            
            try:
                # Handle area of interest selection or creation
                if area_option == 'existing':
                    # Handle year selection
                    year_option = request.form.get('year_option', 'existing')
                    
                    # Get selected area
                    selected_area_id = request.form.get('area_id')
                    if not selected_area_id:
                        flash('Please select an area', 'danger')
                        raise ValueError("No area selected")
                        
                    area = AreaOfInterest.query.get(selected_area_id)
                    if not area:
                        flash('Selected area not found', 'danger')
                        raise ValueError("Area not found")
                    
                    region_code = area.region_code
                    aoi = ee.Geometry.Polygon(eval(region_code.replace('ee.Geometry.Polygon(', '').replace(')', '')))
                    
                    # Assign the region name
                    region_name = area.region_name
                    
                    # Process year selection based on option
                    if year_option == 'existing':
                        selected_year_str = request.form.get('year_select', '')
                    else:  # new year
                        selected_year_str = request.form.get('year_input', '')
                    
                    if not selected_year_str or not selected_year_str.isdigit():
                        flash('Please select or enter a valid year', 'danger')
                        raise ValueError("No valid year provided")
                    
                    selected_year = int(selected_year_str)
                    
                elif area_option == 'new':
                    # Get the manually entered year from the form
                    selected_year_str = request.form.get('new_area_year', '')
                    
                    # Validate that the year is provided and is a valid number
                    if not selected_year_str or not selected_year_str.isdigit():
                        flash('Please enter a valid year', 'danger')
                        raise ValueError("Invalid or missing year")
                    
                    selected_year = int(selected_year_str)
                    
                    # Create new area from coordinates and distance
                    center_lat = request.form.get('center_lat')
                    center_lon = request.form.get('center_lon')
                    distance = request.form.get('distance')
                    
                    # Validate inputs
                    if not all([center_lat, center_lon, distance]):
                        flash('Please provide all required coordinates and distance', 'danger')
                        raise ValueError("Missing coordinates or distance")
                    
                    center_lat = float(center_lat)
                    center_lon = float(center_lon)
                    distance = float(distance) / 2  # Half the side length
                    
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
                    
                    aoi = ee.Geometry.Polygon(coordinates)
                    
                    # Create new area record
                    region_name = request.form.get('region_name')
                    if not region_name:
                        flash('Please provide a name for the region', 'danger')
                        raise ValueError("Missing region name")
                        
                    region_code = f"ee.Geometry.Polygon({coordinates})"
                    new_area = AreaOfInterest(
                        region_name=region_name,
                        region_code=region_code,
                        
                    )
                    db.session.add(new_area)
                    db.session.commit()
                    selected_area_id = new_area.id
                else:
                    flash('Please select an area option', 'danger')
                    raise ValueError("No area option selected")
                
                # Validate year
                if selected_year < 1995 or selected_year >= current_year:
                    flash(f'Please enter a year between 1995 and {current_year - 1}', 'danger')
                    raise ValueError("Year out of valid range")
                
                # Check if data exists for the year and area
                year_data = soil_erosion_estimates.query.filter_by(
                    year=selected_year,
                    area_of_interest_id=selected_area_id
                ).first()
                
                # If data doesn't exist, process it
                if not year_data:
                    processing_message = f"Processing data for year {selected_year}..."
                    
                    # Set dates for processing
                    start_date = f"{selected_year}-01-01"
                    end_date = f"{selected_year}-12-31"
                    output_dir = f"static/images/RusleOutputs/{region_name}/{selected_year}"
                    os.makedirs(output_dir, exist_ok=True)
                    # Process the data using the imported function
                    process_year(selected_year, output_dir, aoi, start_date, end_date)
                    download_aoi_visualizations_gee(region_name, aoi, output_dir)
                    # Create a new soil_erosion_estimates record
                    year_data = soil_erosion_estimates(
                        year=selected_year,
                        area_of_interest_id=selected_area_id,
                        r_factor_image=f"{output_dir}/{selected_year}_R.tif",
                        k_factor_image=f"{output_dir}/{selected_year}_K.tif",
                        ls_factor_image=f"{output_dir}/{selected_year}_LS.tif",
                        c_factor_image=f"{output_dir}/{selected_year}_C.tif",
                        p_factor_image=f"{output_dir}/{selected_year}_P.tif",
                        soil_loss_image=f"{output_dir}/{selected_year}_soil_loss.tif",
                        r_factor_stats=f"{output_dir}/{selected_year}_R_stats.png",
                        k_factor_stats=f"{output_dir}/{selected_year}_K_stats.png",
                        ls_factor_stats=f"{output_dir}/{selected_year}_LS_stats.png",
                        c_factor_stats=f"{output_dir}/{selected_year}_C_stats.png",
                        p_factor_stats=f"{output_dir}/{selected_year}_P_stats.png",
                        soil_loss_stats=f"{output_dir}/{selected_year}_soil_loss_stats.png",
                        soil_loss_detailed_stats=f"{output_dir}/{selected_year}_soil_loss_detailed_analysis.png",
                        aoi_image=f"{output_dir}/aoi_visualization/{region_name}_terrain.tif"
                    )
                    db.session.add(year_data)
                    db.session.commit()
                    
                    processing_message = f"Processing completed for year {selected_year}!"
                
                # Collect all image paths for the selected year
                images = [
                    {"label": "R Factor", "url": convert_image_to_jpg(year_data.r_factor_image), "stats_url": year_data.r_factor_stats},
                    {"label": "K Factor", "url": convert_image_to_jpg(year_data.k_factor_image), "stats_url": year_data.k_factor_stats},
                    {"label": "LS Factor", "url": convert_image_to_jpg(year_data.ls_factor_image), "stats_url": year_data.ls_factor_stats},
                    {"label": "C Factor", "url": convert_image_to_jpg(year_data.c_factor_image), "stats_url": year_data.c_factor_stats},
                    {"label": "P Factor", "url": convert_image_to_jpg(year_data.p_factor_image), "stats_url": year_data.p_factor_stats},
                    {"label": "Soil Loss", "url": convert_image_to_jpg(year_data.soil_loss_image), "stats_url": year_data.soil_loss_stats},
                    {"label": "Area of interest Image", "url":convert_image_to_jpg(year_data.aoi_image),"stats_url": None},
                    {"label": "Detailed Soil Loss stats", "url":year_data.soil_loss_detailed_stats}
                    
                ]
                
                # Filter out any None values
                images = [img for img in images if img.get('url') is not None]
                
            except ValueError as e:
                flash(f'Invalid input: {str(e)}', 'danger')
            except Exception as e:
                flash(f'An error occurred: {str(e)}', 'danger')
                import traceback
                print(traceback.format_exc())
        
        elif model_mode == 'prediction':
            # Handle prediction mode logic here
            try:
                selected_area_id = request.form.get('prediction_area_id')
                prediction_year = request.form.get('prediction_year')

                if not selected_area_id:
                    flash('Please select an area for prediction', 'danger')
                    raise ValueError("No area selected for prediction")

                if not prediction_year or not prediction_year.isdigit():
                    flash('Please enter a valid year for prediction', 'danger')
                    raise ValueError("No valid year provided for prediction")

                try:
                    year = int(prediction_year)
                    if year < current_year or year > 2050:
                        flash(f'Prediction year must be between {current_year} and 2050', 'danger')
                        raise ValueError("Year out of valid range")
                except ValueError:
                    flash('Invalid year format. Please enter a valid number.', 'danger')
                    raise

                area = AreaOfInterest.query.get(selected_area_id)
                if not area:
                    flash('Selected area not found', 'danger')
                    raise ValueError("Area not found")

                region_name = area.region_name
                region_code = area.region_code

                processing_message = f"Processing prediction for {region_name} in {year}..."

                # Load trained model
                encoder, generator, region_to_idx = load_trained_model()

                output_dir = f"static/images/{region_name}/prediction_{year}"
                os.makedirs(output_dir, exist_ok=True)

                # Generate prediction factors
                generated_dir = generate_rusle_factors(
                    year, region_name, encoder, generator, torch.device('cpu'), output_dir, region_to_idx
                )

                # Collect all generated images
                images = []
                for factor in ['R', 'K', 'LS', 'C', 'P', 'soil_loss']:
                    tif_path = os.path.join(generated_dir, f"{year}_{factor}.tif")
                    if os.path.exists(tif_path):
                        images.append({
                            "label": f"{factor} Factor (Predicted {year})",
                            "url": convert_image_to_jpg(tif_path),
                            "stats_url": None
                        })

                # Set selected year for template use
                selected_year = year
                processing_message = f"Prediction completed for {region_name} in {year}!"

            except ValueError as e:
                flash(f'Invalid input: {str(e)}', 'danger')
            except Exception as e:
                flash(f'An error occurred: {str(e)}', 'danger')
                import traceback
                print(traceback.format_exc())
    
    # Get list of available years for the dropdown
    # For each area, get the available years
    area_years = {}
    for area in areas:
        years = [result.year for result in soil_erosion_estimates.query.filter_by(
            area_of_interest_id=area.id).order_by(soil_erosion_estimates.year).all()]
        area_years[area.id] = years
    
    # Get all available years across all areas
    available_years = sorted(list(set([
        year for years_list in area_years.values() for year in years_list
    ])))
    
    return render_template(
        'visualization.html',
        logged_in=logged_in,
        username=username,
        role=role,
        images=images,
        selected_year=selected_year,
        available_years=available_years,
        area_years=area_years,
        areas=areas,
        selected_area_id=selected_area_id,
        processing_message=processing_message,
        model_mode=model_mode,
        current_year=current_year
    )
    
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################

import re

def clean_text(text):
    if not text:
        return ''
    # Replace known bad Unicode characters
    text = text.replace('\u2014', '-').replace('\u2013', '-') \
               .replace('\u2018', "'").replace('\u2019', "'") \
               .replace('\u201C', '"').replace('\u201D', '"') \
               .replace('\u2026', '...') \
               .replace('&nbsp;', ' ')
    # Strip extra spaces
    text = text.strip()

    # Break long words (very important!)
    max_word_length = 30  # Max allowed continuous characters
    text = re.sub(r'(\S{' + str(max_word_length) + r',})', r'\1 ', text)

    return text


def convert_image_to_jpg(image_path):
    try:
        # Open the TIF image
        image = Image.open(image_path)

        # Convert the image to JPEG format
        jpg_image = BytesIO()
        image.save(jpg_image, format='JPEG')

        # Return the JPEG image data as a URL
        return f'data:image/jpeg;base64,{base64.b64encode(jpg_image.getvalue()).decode()}'
    except Exception as e:
        flash(f'Error converting image: {str(e)}', 'danger')
        return None
###############################################################################################################################################################################################
#                                                                                                                                                                                             #
###############################################################################################################################################################################################


if __name__ == '__main__':
    #init_db()  # Initialize the database before running the app
    app.run(debug=True)
