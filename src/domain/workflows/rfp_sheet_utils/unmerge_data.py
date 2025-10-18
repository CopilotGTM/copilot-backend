import concurrent.futures
from typing import Any


def process_sheet(sheet_name: str, sheet) -> dict[str, Any]:
    """
    Process a single sheet and return its data.

    Args:
        sheet_name: Name of the sheet
        sheet: openpyxl worksheet object

    Returns:
        dict: Dictionary containing sheet name and row-wise data
    """
    # Get headers from first row - do this once
    headers = [cell.value for cell in sheet[1]]

    # Pre-compute merged cell ranges and their values
    merged_cell_values = {}
    for merged_range in sheet.merged_cells.ranges:
        # Get the value from the top-left cell of the merged range
        top_left_value = sheet.cell(row=merged_range.min_row, column=merged_range.min_col).value
        # Store this value for all cells in the merged range
        for row in range(merged_range.min_row, merged_range.max_row + 1):
            for col in range(merged_range.min_col, merged_range.max_col + 1):
                cell_coordinate = sheet.cell(row=row, column=col).coordinate
                merged_cell_values[cell_coordinate] = top_left_value

    data = []
    # Get data from remaining rows
    for row in sheet.iter_rows(min_row=2):
        row_data = {}
        for header, cell in zip(headers, row, strict=False):
            # Use the pre-computed merged cell value if it exists, otherwise use the cell's value
            row_data[header] = merged_cell_values.get(cell.coordinate, cell.value)
        data.append(row_data)

    return {"sheetname": sheet_name, "data": data}


def get_unmerged_cell_data(workbook):
    """
    Process a workbook and return a list of objects containing sheet data.
    Handles merged cells by getting values from the top-left cell of merged ranges.
    Processes sheets in parallel for better performance while maintaining original sheet order.

    Args:
        workbook: An openpyxl workbook object

    Returns:
        list: List of objects containing sheet name and row-wise data in original sheet order
    """
    # Create a dictionary to store results by sheet name
    results_dict = {}

    # Use ThreadPoolExecutor to process sheets in parallel
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Create future tasks for each sheet
        future_to_sheet = {
            executor.submit(process_sheet, sheet_name, workbook[sheet_name]): sheet_name
            for sheet_name in workbook.sheetnames
        }

        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_sheet):
            try:
                sheet_result = future.result()
                results_dict[sheet_result["sheetname"]] = sheet_result
            except Exception as e:
                print(f"Error processing sheet {future_to_sheet[future]}: {e}")

    # Return results in original sheet order
    return [results_dict[sheet_name] for sheet_name in workbook.sheetnames]
