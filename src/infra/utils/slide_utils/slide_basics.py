import os
from zipfile import ZipFile


class PptxBasicSlideManager:
    def __init__(self, pptx_path):
        self.pptx_path = pptx_path

    def get_slide_count(self):
        """Returns the total number of slides in the presentation."""
        with ZipFile(self.pptx_path, "r") as zip_read:
            # Count files matching the pattern 'ppt/slides/slide*.xml'
            slide_files = [f for f in zip_read.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")]
            return len(slide_files)

    def read_slide_xml(self, slide_number):
        """Reads and returns the raw XML content of the specified slide."""
        slide_path = f"ppt/slides/slide{slide_number}.xml"
        with ZipFile(self.pptx_path, "r") as zip_read:
            if slide_path not in zip_read.namelist():
                raise FileNotFoundError(f"Slide {slide_number} not found in the presentation.")
            xml_bytes = zip_read.read(slide_path)
            return xml_bytes.decode("utf-8")  # Return as string for easy manipulation

    def delete_slide(self, slide_number):
        """Deletes a specific slide from the presentation.

        Args:
            slide_number (int): The number of the slide to delete.

        Raises:
            FileNotFoundError: If the specified slide doesn't exist.
        """
        slide_path = f"ppt/slides/slide{slide_number}.xml"
        temp_path = self.pptx_path + ".temp"

        with ZipFile(self.pptx_path, "r") as zip_read:
            if slide_path not in zip_read.namelist():
                raise FileNotFoundError(f"Slide {slide_number} not found in the presentation.")

            with ZipFile(temp_path, "w") as zip_write:
                for item in zip_read.infolist():
                    if item.filename != slide_path:
                        zip_write.writestr(item, zip_read.read(item.filename))

        os.replace(temp_path, self.pptx_path)
