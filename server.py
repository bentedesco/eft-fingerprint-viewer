#!/usr/bin/env python3
"""
EFT Viewer Backend Server
Handles EFT file parsing and fingerprint image extraction using NBIS tools.
"""

import os
import sys
import json
import tempfile
import subprocess
import shutil
import base64
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import cgi
import io
import re

# Configuration
NBIS_BIN_PATH = "/tmp/nbis-build/bin"
OPJ_DECOMPRESS = "opj_decompress"
AN2KTOOL = os.path.join(NBIS_BIN_PATH, "an2ktool")
DWSQ = os.path.join(NBIS_BIN_PATH, "dwsq")
PORT = 8888

# ANSI/NIST separators
FS_CHAR = 0x1C  # File Separator - Record delimiter
GS_CHAR = 0x1D  # Group Separator - Field delimiter
RS_CHAR = 0x1E  # Record Separator - Subfield delimiter
US_CHAR = 0x1F  # Unit Separator - Item delimiter


def parse_eft_metadata(eft_path: str, temp_dir: str = None) -> dict:
    """Parse EFT file metadata using an2ktool."""
    result = {
        "transaction": {},
        "demographics": {},
        "records": [],
        "raw_fields": [],
        "fingerprint_records": [],
        "validation": {}
    }
    
    # Change to temp directory to avoid temp file conflicts
    original_dir = os.getcwd()
    try:
        if temp_dir:
            os.chdir(temp_dir)
            # Clean any existing temp files
            for f in Path(temp_dir).glob("fld_*.tmp"):
                try:
                    f.unlink()
                except:
                    pass
        
        proc = subprocess.run(
            [AN2KTOOL, "-print", "all", eft_path],
            capture_output=True,
            text=True
        )
        output = proc.stdout + proc.stderr
        
        current_record_type = None
        
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            # Parse field output: 1.1.1.1 [1.001]=value
            match = re.match(r'(\d+\.\d+\.\d+\.\d+)\s+\[(\d+\.\d+)\]=(.*)$', line)
            if match:
                indices, tag, value = match.groups()
                # Strip control characters from value
                value = value.strip().rstrip('\x1f\x1e\x1d\x1c')
                record_type = tag.split('.')[0]
                field_num = tag.split('.')[1]
                
                result["raw_fields"].append({
                    "indices": indices,
                    "tag": tag,
                    "value": value[:200] if len(value) > 200 else value  # Truncate large values
                })
                
                # Extract key fields
                if record_type == "1":
                    if field_num == "004":
                        result["transaction"]["type"] = value
                    elif field_num == "005":
                        result["transaction"]["date"] = format_date(value)
                    elif field_num == "007":
                        result["transaction"]["dest_agency"] = value
                    elif field_num == "008":
                        result["transaction"]["orig_agency"] = value
                    elif field_num == "009":
                        result["transaction"]["tcn"] = value
                        
                elif record_type == "2":
                    if field_num == "018":
                        result["demographics"]["name"] = value
                    elif field_num == "020":
                        result["demographics"]["pob"] = value
                    elif field_num == "022":
                        result["demographics"]["dob"] = format_date(value)
                    elif field_num == "024":
                        result["demographics"]["sex"] = {"M": "Male", "F": "Female"}.get(value, value)
                    elif field_num == "025":
                        result["demographics"]["race"] = decode_race(value)
                    elif field_num == "027":
                        result["demographics"]["height"] = format_height(value)
                    elif field_num == "029":
                        result["demographics"]["weight"] = f"{value} lbs"
                    elif field_num == "031":
                        result["demographics"]["eyes"] = decode_eye_color(value)
                    elif field_num == "032":
                        result["demographics"]["hair"] = decode_hair_color(value)
                    elif field_num == "037":
                        result["demographics"]["reason"] = value
                    elif field_num == "038":
                        result["demographics"]["date_printed"] = format_date(value)
                    elif field_num == "041":
                        result["demographics"]["address"] = value
                        
                elif record_type == "14":
                    if field_num == "011":
                        # Track compression type
                        if not result.get("compression"):
                            result["compression"] = value
                    elif field_num == "013":
                        # Finger position - track which fingers are present
                        try:
                            fp = int(value)
                            result["fingerprint_records"].append({
                                "position": fp,
                                "name": FINGER_POSITION_NAMES.get(fp, f"Unknown ({fp})")
                            })
                        except:
                            pass
                        
    except Exception as e:
        result["error"] = str(e)
    finally:
        if temp_dir:
            os.chdir(original_dir)
    
    # Validate the EFT file
    result["validation"] = validate_eft(result)
    
    return result


# Finger position names per ANSI/NIST-ITL standard
FINGER_POSITION_NAMES = {
    0: "Unknown",
    1: "Right Thumb",
    2: "Right Index",
    3: "Right Middle",
    4: "Right Ring",
    5: "Right Little",
    6: "Left Thumb",
    7: "Left Index",
    8: "Left Middle",
    9: "Left Ring",
    10: "Left Little",
    11: "Plain Right Thumb",
    12: "Plain Left Thumb",
    13: "Plain Right Four",
    14: "Plain Left Four",
    15: "Plain Thumbs (Both)"
}

# Required fields per EBTS for different transaction types
EBTS_REQUIREMENTS = {
    "FAUF": {  # Applicant Fingerprint (ATF Form 1/4)
        "description": "FBI Applicant Fingerprint (ATF eForm)",
        "required_demographics": ["name", "dob", "sex", "race", "height", "weight", "eyes", "hair"],
        "fingerprint_options": [
            {
                "name": "Complete FD-258 (Rolled + Slaps)",
                "required": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 15],
                "description": "All 10 rolled prints plus 3 flat impressions"
            },
            {
                "name": "Rolled Prints Only",
                "required": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "description": "All 10 individual rolled fingerprints"
            },
            {
                "name": "Flat/Slap Impressions Only",
                "required": [13, 14, 15],
                "description": "Plain impressions (4-finger slaps + thumbs)"
            }
        ]
    }
}


def validate_eft(metadata: dict) -> dict:
    """Validate EFT file against EBTS requirements."""
    validation = {
        "is_valid": False,
        "transaction_type": None,
        "fingerprints_present": [],
        "fingerprints_missing": [],
        "demographics_present": [],
        "demographics_missing": [],
        "match_type": None,
        "messages": [],
        "warnings": []
    }
    
    # Get transaction type
    txn_type = metadata.get("transaction", {}).get("type", "").upper()
    validation["transaction_type"] = txn_type
    
    # Get present finger positions
    present_positions = set()
    for fp in metadata.get("fingerprint_records", []):
        present_positions.add(fp["position"])
        validation["fingerprints_present"].append(fp)
    
    # Get requirements for this transaction type
    requirements = EBTS_REQUIREMENTS.get(txn_type, EBTS_REQUIREMENTS.get("FAUF"))
    
    # Check fingerprint requirements - see which option is satisfied
    best_match = None
    best_missing = None
    
    for option in requirements.get("fingerprint_options", []):
        required = set(option["required"])
        missing = required - present_positions
        
        if len(missing) == 0:
            # This option is fully satisfied
            validation["is_valid"] = True
            validation["match_type"] = option["name"]
            validation["messages"].append(f"✓ File contains valid {option['name']}")
            best_match = option
            best_missing = []
            break
        elif best_missing is None or len(missing) < len(best_missing):
            best_match = option
            best_missing = list(missing)
    
    # If no option is fully satisfied, report what's missing
    if not validation["is_valid"] and best_match:
        validation["match_type"] = f"Incomplete - closest to {best_match['name']}"
        for pos in sorted(best_missing):
            validation["fingerprints_missing"].append({
                "position": pos,
                "name": FINGER_POSITION_NAMES.get(pos, f"Position {pos}")
            })
        validation["messages"].append(f"⚠ Missing {len(best_missing)} fingerprint(s) for {best_match['name']}")
    
    # Check what extra fingerprints we have that aren't in any required set
    all_required = set()
    for option in requirements.get("fingerprint_options", []):
        all_required.update(option["required"])
    
    extra_positions = present_positions - all_required
    if extra_positions:
        for pos in sorted(extra_positions):
            validation["warnings"].append(f"Extra fingerprint at position {pos}: {FINGER_POSITION_NAMES.get(pos, 'Unknown')}")
    
    # Check demographics
    demographics = metadata.get("demographics", {})
    required_demo = requirements.get("required_demographics", [])
    
    for field in required_demo:
        if demographics.get(field):
            validation["demographics_present"].append(field)
        else:
            validation["demographics_missing"].append(field)
    
    if validation["demographics_missing"]:
        validation["messages"].append(f"⚠ Missing {len(validation['demographics_missing'])} demographic field(s)")
        if validation["is_valid"]:
            validation["is_valid"] = False  # Downgrade to invalid if missing required demographics
    else:
        validation["messages"].append("✓ All required demographic fields present")
    
    # Final summary
    if validation["is_valid"]:
        validation["messages"].insert(0, "✓ FILE IS VALID")
    else:
        validation["messages"].insert(0, "✗ FILE IS INCOMPLETE")
    
    return validation


def extract_fingerprint_images(eft_path: str, output_dir: str) -> list:
    """Extract fingerprint images from EFT file using NBIS tools."""
    images = []
    
    # Clean any existing temp files in output dir
    for f in Path(output_dir).glob("fld_*.tmp"):
        try:
            f.unlink()
        except:
            pass
    for f in Path(output_dir).glob("fld_*.jp2"):
        try:
            f.unlink()
        except:
            pass
    
    # Run an2ktool which creates temp files
    original_dir = os.getcwd()
    
    try:
        os.chdir(output_dir)
        
        # an2ktool extracts images as .tmp files
        subprocess.run(
            [AN2KTOOL, "-print", "all", eft_path],
            capture_output=True,
            text=True
        )
        
        # Find extracted temp files
        tmp_files = sorted(Path(output_dir).glob("fld_*.tmp"))
        
        for tmp_file in tmp_files:
            # Detect format
            with open(tmp_file, 'rb') as f:
                header = f.read(12)
            
            output_png = tmp_file.with_suffix('.png')
            
            # Check if JPEG 2000 (JP2)
            if header[:12] == b'\x00\x00\x00\x0cjP  \r\n\x87\n':
                # Rename to .jp2 for opj_decompress
                jp2_file = tmp_file.with_suffix('.jp2')
                shutil.copy(tmp_file, jp2_file)
                
                # Decompress JP2 to PNG
                result = subprocess.run(
                    [OPJ_DECOMPRESS, "-i", str(jp2_file), "-o", str(output_png)],
                    capture_output=True,
                    text=True
                )
                
                if output_png.exists():
                    images.append({
                        "filename": output_png.name,
                        "path": str(output_png),
                        "format": "JPEG 2000",
                        "original": tmp_file.name
                    })
                    
            # Check if WSQ
            elif header[:2] == b'\xff\xa0':
                # Use NBIS dwsq to convert WSQ to raw, then to PNG
                raw_file = tmp_file.with_suffix('.raw')
                result = subprocess.run(
                    [DWSQ, "raw", "-r", str(tmp_file), "-o", str(raw_file)],
                    capture_output=True,
                    text=True
                )
                
                # Convert raw to PNG using ImageMagick if available
                if raw_file.exists():
                    images.append({
                        "filename": raw_file.name,
                        "path": str(raw_file),
                        "format": "WSQ (raw)",
                        "original": tmp_file.name
                    })
                    
            # Check if JPEG
            elif header[:3] == b'\xff\xd8\xff':
                # Already JPEG, just rename
                jpg_file = tmp_file.with_suffix('.jpg')
                shutil.copy(tmp_file, jpg_file)
                images.append({
                    "filename": jpg_file.name,
                    "path": str(jpg_file),
                    "format": "JPEG",
                    "original": tmp_file.name
                })
                
    finally:
        os.chdir(original_dir)
    
    return images


def format_date(date_str: str) -> str:
    """Format YYYYMMDD to YYYY-MM-DD."""
    if len(date_str) >= 8:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    return date_str


def format_height(height_str: str) -> str:
    """Format height from inches to feet'inches\"."""
    if len(height_str) >= 3:
        feet = height_str[0]
        inches = int(height_str[1:])
        return f"{feet}'{inches}\""
    return height_str


def decode_race(code: str) -> str:
    races = {
        'A': 'Asian', 'B': 'Black', 'I': 'American Indian',
        'W': 'White', 'P': 'Pacific Islander', 'H': 'Hispanic', 'U': 'Unknown'
    }
    return races.get(code, code)


def decode_eye_color(code: str) -> str:
    colors = {
        'BLK': 'Black', 'BLU': 'Blue', 'BRO': 'Brown', 'GRY': 'Gray',
        'GRN': 'Green', 'HAZ': 'Hazel', 'MAR': 'Maroon', 'PNK': 'Pink'
    }
    return colors.get(code, code)


def decode_hair_color(code: str) -> str:
    colors = {
        'BLK': 'Black', 'BLN': 'Blonde', 'BRO': 'Brown', 'GRY': 'Gray',
        'RED': 'Red', 'SDY': 'Sandy', 'WHI': 'White', 'BAL': 'Bald'
    }
    return colors.get(code, code)


def image_to_base64(image_path: str) -> str:
    """Convert image file to base64 data URL."""
    with open(image_path, 'rb') as f:
        data = f.read()
    
    # Determine MIME type
    ext = Path(image_path).suffix.lower()
    mime_types = {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif'
    }
    mime_type = mime_types.get(ext, 'application/octet-stream')
    
    b64 = base64.b64encode(data).decode('utf-8')
    return f"data:{mime_type};base64,{b64}"


class EFTViewerHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for EFT Viewer."""
    
    def __init__(self, *args, **kwargs):
        # Set directory to serve static files from
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_POST(self):
        """Handle POST requests for file upload."""
        if self.path == '/api/parse':
            self.handle_parse_request()
        else:
            self.send_error(404, 'Not Found')
    
    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        
        if parsed.path == '/api/health':
            self.send_json_response({"status": "ok", "nbis": os.path.exists(AN2KTOOL)})
        elif parsed.path.startswith('/output/'):
            # Serve extracted images
            self.serve_output_file(parsed.path[8:])
        else:
            # Serve static files
            super().do_GET()
    
    def handle_parse_request(self):
        """Parse uploaded EFT file."""
        content_type = self.headers.get('Content-Type', '')
        
        if 'multipart/form-data' in content_type:
            # Parse multipart form data
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': content_type
                }
            )
            
            if 'file' not in form:
                self.send_json_response({"error": "No file uploaded"}, 400)
                return
            
            file_item = form['file']
            file_data = file_item.file.read()
            file_name = file_item.filename
        else:
            # Raw file upload
            content_length = int(self.headers.get('Content-Length', 0))
            file_data = self.rfile.read(content_length)
            file_name = 'uploaded.eft'
        
        # Create temp directory for processing
        with tempfile.TemporaryDirectory() as temp_dir:
            eft_path = os.path.join(temp_dir, file_name)
            
            # Save uploaded file
            with open(eft_path, 'wb') as f:
                f.write(file_data)
            
            # Parse metadata
            metadata = parse_eft_metadata(eft_path, temp_dir)
            
            # Extract images
            images = extract_fingerprint_images(eft_path, temp_dir)
            
            # Convert images to base64 for response
            fingerprints = []
            for img in images:
                if os.path.exists(img['path']):
                    try:
                        b64_data = image_to_base64(img['path'])
                        fingerprints.append({
                            "name": img['filename'],
                            "format": img['format'],
                            "data": b64_data
                        })
                    except Exception as e:
                        fingerprints.append({
                            "name": img['filename'],
                            "format": img['format'],
                            "error": str(e)
                        })
            
            response = {
                "filename": file_name,
                "metadata": metadata,
                "fingerprints": fingerprints
            }
            
            self.send_json_response(response)
    
    def serve_output_file(self, filename):
        """Serve a file from the output directory."""
        output_dir = Path(__file__).parent / 'output'
        file_path = output_dir / filename
        
        if not file_path.exists() or not file_path.is_relative_to(output_dir):
            self.send_error(404, 'File not found')
            return
        
        # Determine content type
        ext = file_path.suffix.lower()
        content_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif'
        }
        content_type = content_types.get(ext, 'application/octet-stream')
        
        with open(file_path, 'rb') as f:
            data = f.read()
        
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(data))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)
    
    def send_json_response(self, data, status=200):
        """Send JSON response."""
        response = json.dumps(data)
        encoded = response.encode('utf-8')
        
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(encoded))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(encoded)


def main():
    """Run the server."""
    print(f"EFT Viewer Server")
    print(f"=" * 50)
    print(f"NBIS Tools Path: {NBIS_BIN_PATH}")
    print(f"an2ktool: {'✓ Found' if os.path.exists(AN2KTOOL) else '✗ Not found'}")
    print(f"opj_decompress: {'✓ Found' if shutil.which(OPJ_DECOMPRESS) else '✗ Not found'}")
    print(f"=" * 50)
    
    os.chdir(Path(__file__).parent)
    
    server = HTTPServer(('', PORT), EFTViewerHandler)
    print(f"\nServer running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
