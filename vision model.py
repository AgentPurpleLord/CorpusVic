import torch
import logging
import time

from docling.datamodel.accelerator_options import (
    AcceleratorDevice,
    AcceleratorOptions,
)
from docling.datamodel.base_models import InputFormat
from docling.datamodel.settings import settings
from docling.datamodel.pipeline_options import VlmPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    VlmConvertOptions,
    VlmPipelineOptions,
)
from docling.datamodel.vlm_engine_options import (
    MlxVlmEngineOptions,
    TransformersVlmEngineOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logging.getLogger("docling").setLevel(logging.INFO)

logger = logging.getLogger("corpus")


# ---------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------

print(f"PyTorch version: {torch.__version__}")
print(f"CUDA available: {torch.cuda.is_available()}")
print(f"CUDA version: {torch.version.cuda}")

if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
else:
    print("GPU: None")

# Check GPU memory
if torch.cuda.is_available():
    print(f"GPU Memory allocated: {torch.cuda.memory_allocated(0) / 1024**3:.2f} GB")
    print(f"GPU Memory reserved: {torch.cuda.memory_reserved(0) / 1024**3:.2f} GB")

# ---------------------------------------------------------
# Enable Docling profiling
# ---------------------------------------------------------

settings.debug.profile_pipeline_timings = True
settings.perf.page_batch_size = 24

source = "acts/crimes-act-50.pdf"

pipeline_options = VlmPipelineOptions(
    accelerator_options=AcceleratorOptions(
        device=AcceleratorDevice.CUDA,
    ),
    vlm_options= VlmConvertOptions.from_preset("granite_docling"),
)


converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_cls=VlmPipeline,
            pipeline_options=pipeline_options,
        ),
    }
)

# ---------------------------------------------------------
# Initialise
# ---------------------------------------------------------

logger.info("Initialising Docling pipeline...")

start = time.perf_counter()

converter.initialize_pipeline(InputFormat.PDF)

elapsed = time.perf_counter() - start

logger.info(
    "Pipeline initialised in %.2f seconds",
    elapsed,
)

# ---------------------------------------------------------
# Convert
# ---------------------------------------------------------

logger.info("Starting conversion: %s", source)

start = time.perf_counter()

result = converter.convert(source)

elapsed = time.perf_counter() - start

logger.info(
    "Conversion finished in %.2f seconds",
    elapsed,
)

# ---------------------------------------------------------
# Results
# ---------------------------------------------------------

doc = result.document

logger.info(
    "Conversion status: %s",
    result.status,
)

logger.info(
    "Pages processed: %d",
    len(result.pages),
)

# ---------------------------------------------------------
# Timing information
# ---------------------------------------------------------

if result.timings:
    logger.info("Pipeline timings:")

    for name, timing in result.timings.items():
        logger.info(
            "  %-25s %s",
            name,
            timing,
        )


# ---------------------------------------------------------
# Export
# ---------------------------------------------------------

logger.info("Writing Markdown...")

doc.save_as_markdown("output.md")

logger.info("Writing JSON...")

doc.save_as_json("output.json")

logger.info("Done.")