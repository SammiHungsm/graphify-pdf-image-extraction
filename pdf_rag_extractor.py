#!/usr/bin/env python3
"""
SFC Financial Report PDF Extractor (NVIDIA API Edition)
專為香港上市公司年報設計：精準抽取純文字、過濾雜訊圖片、智能裁切 Vector 圖表，並交由 Vision LLM 解讀。
支援單一檔案或整個資料夾的批次處理。
prompt:
/graphify ./raw --mode deep 
Map the logical relationships between these entities (e.g., ReportingEntity -> operates in -> MarketGeography; KeyPersonnel -> member of -> Committee). Ensure `stock_code` is precisely extracted.
"""

import os
import sys
import base64
import hashlib
import argparse
import requests
from pathlib import Path
from typing import List, Tuple, Optional

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: 搵唔到 PyMuPDF！請先執行: pip install PyMuPDF")
    sys.exit(1)


def extract_chart_area(page: fitz.Page, min_paths: int = 60) -> Optional[Tuple[bytes, dict]]:
    """
    智能探測：找出 Vector 圖表喺頁面中嘅精準位置 (Bounding Box)，並局部 Render 成高清圖。
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return None
    
    if len(drawings) < min_paths:
        return None
        
    page_area = page.rect.get_area()
    chart_rect = None
    valid_paths = 0
    
    for d in drawings:
        rect = d.get("rect")
        if not rect:
            continue
        
        if rect.get_area() > page_area * 0.8:
            continue
            
        if valid_paths == 0:
            chart_rect = fitz.Rect(rect)
        else:
            chart_rect |= rect  
            
        valid_paths += 1
    
    if valid_paths >= min_paths and chart_rect and chart_rect.get_area() > 20000:
        chart_rect = chart_rect + (-15, -15, 15, 15)
        chart_rect = chart_rect.intersect(page.rect)
        
        zoom_matrix = fitz.Matrix(300/72, 300/72)
        pix = page.get_pixmap(matrix=zoom_matrix, clip=chart_rect)
        
        image_bytes = pix.tobytes("png")
        metadata = {
            "rect": chart_rect,
            "width": pix.width,
            "height": pix.height,
            "paths_count": valid_paths
        }
        return (image_bytes, metadata)
        
    return None


def extract_images_from_pdf(pdf_path: Path, output_dir: Path) -> List[Tuple[int, int, Path, bytes]]:
    """
    強力雜訊過濾：抽出底層真實圖片 (JPG/PNG)，隔走 Logo、背景、裝飾線。
    """
    images_dir = output_dir / "extracted_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    
    extracted_images = []
    seen_hashes = set()
    
    doc = fitz.open(str(pdf_path))
    
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        image_list = page.get_images(full=True)
        
        for img_index, img in enumerate(image_list, start=1):
            xref = img[0]
            width = img[2]
            height = img[3]
            
            if width < 150 or height < 150:
                continue
            aspect_ratio = max(width, height) / min(width, height)
            if aspect_ratio > 8:
                continue
            if width > 1800 and height > 2500:
                continue

            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]
            
            if len(image_bytes) < 10000:  
                continue
            
            image_hash = hashlib.md5(image_bytes).hexdigest()[:8]
            if image_hash in seen_hashes:
                continue
            seen_hashes.add(image_hash)
            
            image_filename = f"{pdf_path.stem}_p{page_num+1}_img{img_index}_{image_hash}.{image_ext}"
            image_path = images_dir / image_filename
            image_path.write_bytes(image_bytes)
            
            extracted_images.append((page_num, img_index, image_path, image_bytes))
            
    doc.close()
    return extracted_images


def describe_image_with_vision(image_bytes: bytes, mime_type: str = "image/png", api_key: str = None, invoke_url: str = None, model: str = None) -> str:
    """
    呼叫 NVIDIA API 進行解讀，使用 requests 發送 base64 圖片
    """
    if not api_key:
        return "[未設定 API Key，跳過圖片描述]"
        
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json"
        }
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """text": "Act as a Senior Financial Data Architect. Perform a forensic multimodal analysis of this image: "
        "1. Visual Classification: Identify the visual type (e.g., Grouped Bar Chart, Stacked Bar Chart, Table, Diagram). Explicitly state if it contains single or multiple data series. "
        "2. Legend & Series Mapping: Map every color, pattern, and marker to its exact legend category or metric. Do not conflate series (e.g., 'Blue = Revenue', 'Green = Net Profit'). "
        "3. Multi-Dimensional Extraction: Extract all raw data points systematically. For multi-parameter or grouped charts, you MUST extract the value for EVERY series at EACH axis intersection (e.g., 'For 2023: Revenue = X, Profit = Y'). Include all X/Y axes, specific data labels, and percentages. "
        "4. Structural Relationships: Describe hierarchies, consolidations, or entity relationships depicted. "
        "5. Visual Nuances: Note any trends, annotations, or secondary axes (e.g., a line graph overlaid on a bar chart). Output in precise Markdown."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
            "top_p": 1.00,
            "stream": False
        }

        response = requests.post(invoke_url, headers=headers, json=payload)
        response.raise_for_status()
        
        return response.json()["choices"][0]["message"]["content"]
        
    except requests.exceptions.HTTPError as e:
        return f"[圖片描述失敗 (HTTP Error): {e.response.text}]"
    except Exception as e:
        return f"[圖片描述失敗: {e}]"


def get_existing_images_info(output_dir: Path, pdf_stem: str) -> dict:
    """
    Check if extracted images already exist for a PDF.
    Returns dict with 'vector_charts' and 'embedded_images' lists.
    """
    images_dir = output_dir / "extracted_images"
    if not images_dir.exists():
        return {"vector_charts": [], "embedded_images": [], "has_existing": False}

    def get_page_num(path: Path) -> int:
        try:
            return int(path.stem.split("_p")[1].split("_")[0])
        except:
            return 0

    vector_charts = sorted([
        f for f in images_dir.iterdir()
        if f.name.startswith(f"{pdf_stem}_p") and "vector_chart" in f.name
    ], key=get_page_num)

    embedded_images = sorted([
        f for f in images_dir.iterdir()
        if f.name.startswith(f"{pdf_stem}_p") and "vector_chart" not in f.name
        and f.suffix.lower() in ['.png', '.jpg', '.jpeg']
    ], key=get_page_num)

    has_existing = len(vector_charts) > 0 or len(embedded_images) > 0
    return {
        "vector_charts": vector_charts,
        "embedded_images": embedded_images,
        "has_existing": has_existing,
        "images_dir": images_dir
    }


def process_existing_images_only(pdf_path: Path, output_dir: Path, api_key: str, invoke_url: str, model: str) -> Optional[Path]:
    """
    Resume mode: PDF already processed, only process existing images with Vision LLM.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    images_info = get_existing_images_info(output_dir, pdf_path.stem)

    if not images_info["has_existing"]:
        print(f"[WARN] No existing images found for {pdf_path.stem}, will do full processing")
        return None

    md_output_path = output_dir / f"{pdf_path.stem}_rag_ready.md"
    existing_md = md_output_path.read_text(encoding="utf-8") if md_output_path.exists() else ""

    print(f"\n[RRESUME MODE] Found existing images for: {pdf_path.name}")
    print(f"  Vector charts: {len(images_info['vector_charts'])}")
    print(f"  Embedded images: {len(images_info['embedded_images'])}")

    all_images = [(f, "vector") for f in images_info["vector_charts"]] + \
                 [(f, "embedded") for f in images_info["embedded_images"]]

    new_content = "\n---\n## [Resume] Image Descriptions Added\n\n"

    for img_path, img_type in all_images:
        print(f"  Processing {img_type}: {img_path.name}")
        image_bytes = img_path.read_bytes()
        mime_type = "image/jpeg" if img_path.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
        description = describe_image_with_vision(image_bytes, mime_type, api_key, invoke_url, model)

        page_match = img_path.stem.split("_p")[1].split("_")[0] if "_p" in img_path.stem else "?"
        new_content += f"### {img_type.title()} - Page {page_match} ({img_path.name})\n"
        new_content += f"> **LLM Description:**\n{description}\n\n"

    md_output_path.write_text(existing_md + new_content, encoding="utf-8")
    print(f"[DONE] Updated: {md_output_path}")
    return md_output_path


def process_pdf(pdf_path: Path, output_dir: Path, api_key: str, invoke_url: str, model: str) -> Optional[Path]:
    """處理單一 PDF 的主流程"""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = get_existing_images_info(output_dir, pdf_path.stem)
    if existing["has_existing"]:
        return process_existing_images_only(pdf_path, output_dir, api_key, invoke_url, model)

    md_output_path = output_dir / f"{pdf_path.stem}_rag_ready.md"
    
    print(f"\n📄 正在處理: {pdf_path.name}")
    
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"❌ 無法開啟檔案 {pdf_path.name}: {e}")
        return None

    md_content = f"# {pdf_path.stem}\n\n**Source:** `{pdf_path.name}`\n**Pages:** {len(doc)}\n\n---\n"
    
    print("  掃描文字與 Vector 圖表中...")
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        
        text = page.get_text()
        if text.strip():
            md_content += f"\n## Page {page_num + 1}\n\n{text}\n"
            
        chart_result = extract_chart_area(page)
        if chart_result:
            chart_bytes, chart_meta = chart_result
            print(f"  🎯 第 {page_num + 1} 頁發現複雜圖表 (包含 {chart_meta['paths_count']} 條向量路徑) -> 交給 LLM 解讀")

            images_dir = output_dir / "extracted_images"
            images_dir.mkdir(parents=True, exist_ok=True)
            chart_save_path = images_dir / f"{pdf_path.stem}_p{page_num+1}_vector_chart.png"
            chart_save_path.write_bytes(chart_bytes)
            print(f"  💾 已將 Vector 圖表儲存至: {chart_save_path}")

            description = describe_image_with_vision(chart_bytes, "image/png", api_key, invoke_url, model)
            
            md_content += f"\n### 📊 Vector Chart Detected (Page {page_num + 1})\n"
            md_content += f"> **LLM 解析結果:**\n{description}\n\n"
    
    doc.close()
    
    print("  掃描底層真實圖片 (JPG/PNG)...")
    images = extract_images_from_pdf(pdf_path, output_dir)
    
    if images:
        md_content += "\n---\n## 🖼️ 附錄：文件內的真實圖片解析\n\n"
        for page_num, img_index, image_path, image_bytes in images:
            print(f"  🖼️ 處理第 {page_num+1} 頁的圖片 ({image_path.name}) -> 交給 LLM 解讀")
            
            mime_type = "image/jpeg" if image_path.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
            description = describe_image_with_vision(image_bytes, mime_type, api_key, invoke_url, model)
            
            md_content += f"### 圖片來源: 第 {page_num + 1} 頁\n"
            md_content += f"*(檔案參考: `{image_path.relative_to(output_dir)}`)*\n"
            md_content += f"> **LLM 解析結果:**\n{description}\n\n"
    
    md_output_path.write_text(md_content, encoding="utf-8")
    print(f"✅ 完成！結果已儲存至: {md_output_path}")
    return md_output_path


def main():
    parser = argparse.ArgumentParser(description="Advanced SFC Annual Report PDF Extractor (NVIDIA Edition)")
    parser.add_argument("input_path", help="Path to a single PDF file or a folder containing PDFs")
    parser.add_argument("--output-dir", "-o", default="./processed_docs", help="Output directory")
    parser.add_argument("--api-key", help="NVIDIA API Key")
    parser.add_argument("--invoke-url", default="https://integrate.api.nvidia.com/v1/chat/completions", help="NVIDIA API Endpoint")
    parser.add_argument("--model", default="meta/llama-3.2-90b-vision-instruct", help="Vision Model Name")
    #mistralai/mistral-large-3-675b-instruct-2512
    
    args = parser.parse_args()
    
    api_key = args.api_key or os.environ.get("NVIDIA_API_KEY")
    input_path = Path(args.input_path)
    
    if not input_path.exists():
        print(f"❌ Error: Path does not exist: {input_path}")
        sys.exit(1)
        
    if not api_key:
        print("⚠️ Warning: No NVIDIA API Key provided. Images will NOT be sent to the LLM for description.")
    
    if input_path.is_dir():
        pdf_files = list(input_path.glob("*.pdf")) + list(input_path.glob("*.PDF"))
        total_files = len(pdf_files)
        
        if total_files == 0:
            print(f"⚠️ No PDF files found in directory: {input_path}")
            sys.exit(0)
            
        print(f"📁 Found directory! Preparing to process {total_files} PDF file(s)...")
        print("-" * 50)
        
        # A standard loop over the exact number of files found. 
        # Once it hits the final file, the loop finishes naturally.
        for i, pdf_file in enumerate(pdf_files, 1):
            print(f"\n[{i}/{total_files}] Processing file: {pdf_file.name}")
            process_pdf(pdf_file, args.output_dir, api_key, args.invoke_url, args.model)
            
        print("\n🎉 All PDFs in the directory have been processed successfully!")
        
        # Explicit hard exit to guarantee the script shuts down completely after finishing the folder
        sys.exit(0)
        
    else:
        if input_path.suffix.lower() != ".pdf":
            print(f"❌ Error: {input_path.name} is not a PDF file!")
            sys.exit(1)
            
        print(f"\n[1/1] Processing single file: {input_path.name}")
        process_pdf(input_path, args.output_dir, api_key, args.invoke_url, args.model)
        
        sys.exit(0)

if __name__ == "__main__":
    main()