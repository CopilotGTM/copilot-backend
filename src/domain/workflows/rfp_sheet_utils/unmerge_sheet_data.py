def get_unmerged_sheet_data(workbook, target_sheet_name):
    """
    Process a specific sheet from a workbook and return its data.
    Handles merged cells by getting values from the top-left cell of merged ranges.

    Args:
        workbook: An openpyxl workbook object
        target_sheet_name: Name of the sheet to process

    Returns:
        dict: Object containing sheet name and row-wise data, or None if sheet not found
    """
    if target_sheet_name not in workbook.sheetnames:
        return None

    sheet = workbook[target_sheet_name]
    data = []

    # Get headers from first row
    headers = [cell.value for cell in sheet[1]]

    # Get data from remaining rows
    for row in sheet.iter_rows(min_row=1):
        row_data = {}
        for header, cell in zip(headers, row):
            # Check if the cell is part of a merged range
            is_merged = False
            for merged_range in sheet.merged_cells.ranges:
                if cell.coordinate in merged_range:
                    is_merged = True
                    # Get the value from the top-left cell of the merged range
                    top_left_cell = sheet.cell(
                        row=merged_range.min_row, column=merged_range.min_col
                    )
                    row_data[header] = top_left_cell.value
                    break
            if not is_merged:
                row_data[header] = cell.value
        data.append(row_data)

    return {"sheetname": target_sheet_name, "data": data}
