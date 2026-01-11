#!/bin/bash
#
# EFT Fingerprint Viewer - Setup Script
# 
# This script installs all dependencies required to run the EFT Fingerprint Viewer.
#
# Usage: ./setup.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       EFT Fingerprint Viewer - Setup Script               ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Detect OS
OS="unknown"
if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macos"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="linux"
else
    echo -e "${RED}Error: Unsupported operating system: $OSTYPE${NC}"
    echo "This script supports macOS and Linux only."
    exit 1
fi

echo -e "${GREEN}✓ Detected OS: ${OS}${NC}"
echo ""

# Check Python
echo -e "${BLUE}[1/4] Checking Python...${NC}"
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2)
    echo -e "${GREEN}✓ Python ${PYTHON_VERSION} found${NC}"
else
    echo -e "${RED}✗ Python 3 not found${NC}"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

# Install OpenJPEG
echo ""
echo -e "${BLUE}[2/4] Installing OpenJPEG...${NC}"
if command -v opj_decompress &> /dev/null; then
    echo -e "${GREEN}✓ OpenJPEG already installed${NC}"
else
    if [[ "$OS" == "macos" ]]; then
        if command -v brew &> /dev/null; then
            echo "Installing OpenJPEG via Homebrew..."
            brew install openjpeg
            echo -e "${GREEN}✓ OpenJPEG installed${NC}"
        else
            echo -e "${RED}✗ Homebrew not found${NC}"
            echo "Please install Homebrew first: https://brew.sh"
            exit 1
        fi
    elif [[ "$OS" == "linux" ]]; then
        echo "Installing OpenJPEG via apt..."
        sudo apt-get update
        sudo apt-get install -y libopenjp2-tools
        echo -e "${GREEN}✓ OpenJPEG installed${NC}"
    fi
fi

# Build NBIS
echo ""
echo -e "${BLUE}[3/4] Building NIST NBIS...${NC}"
NBIS_BUILD_DIR="/tmp/nbis-build"
NBIS_SRC_DIR="/tmp/nbis-src"

if [[ -f "${NBIS_BUILD_DIR}/bin/an2ktool" ]]; then
    echo -e "${GREEN}✓ NBIS already built at ${NBIS_BUILD_DIR}${NC}"
else
    echo "Cloning NBIS source..."
    rm -rf "${NBIS_SRC_DIR}"
    git clone --depth 1 https://github.com/biometric-technologies/nist-biometric-image-software-nbis.git "${NBIS_SRC_DIR}"
    
    echo "Building NBIS (this may take a few minutes)..."
    cd "${NBIS_SRC_DIR}"
    mkdir -p "${NBIS_BUILD_DIR}"
    
    # Run setup
    bash setup.sh "${NBIS_BUILD_DIR}" --without-X11 --STDLIBS
    
    # Build
    make config
    make it
    make install LIBNBIS=no
    
    cd - > /dev/null
    
    # Verify installation
    if [[ -f "${NBIS_BUILD_DIR}/bin/an2ktool" ]]; then
        echo -e "${GREEN}✓ NBIS built successfully${NC}"
    else
        echo -e "${RED}✗ NBIS build failed${NC}"
        exit 1
    fi
fi

# Verify all tools
echo ""
echo -e "${BLUE}[4/4] Verifying installation...${NC}"
echo ""

ERRORS=0

# Check an2ktool
if [[ -f "${NBIS_BUILD_DIR}/bin/an2ktool" ]]; then
    echo -e "${GREEN}✓ an2ktool: ${NBIS_BUILD_DIR}/bin/an2ktool${NC}"
else
    echo -e "${RED}✗ an2ktool not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Check dwsq
if [[ -f "${NBIS_BUILD_DIR}/bin/dwsq" ]]; then
    echo -e "${GREEN}✓ dwsq: ${NBIS_BUILD_DIR}/bin/dwsq${NC}"
else
    echo -e "${RED}✗ dwsq not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Check opj_decompress
if command -v opj_decompress &> /dev/null; then
    OPJ_PATH=$(which opj_decompress)
    echo -e "${GREEN}✓ opj_decompress: ${OPJ_PATH}${NC}"
else
    echo -e "${RED}✗ opj_decompress not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Check Python
if command -v python3 &> /dev/null; then
    PYTHON_PATH=$(which python3)
    echo -e "${GREEN}✓ python3: ${PYTHON_PATH}${NC}"
else
    echo -e "${RED}✗ python3 not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo ""

if [[ $ERRORS -eq 0 ]]; then
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║              Setup completed successfully!                 ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "To start the EFT Fingerprint Viewer:"
    echo ""
    echo -e "  ${YELLOW}python3 server.py${NC}"
    echo ""
    echo -e "Then open your browser to:"
    echo ""
    echo -e "  ${BLUE}http://localhost:8888${NC}"
    echo ""
else
    echo -e "${RED}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║              Setup completed with errors!                  ║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Please resolve the issues above and try again."
    exit 1
fi
