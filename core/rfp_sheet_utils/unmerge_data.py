def get_unmerged_cell_data(workbook):
    """
    Process a workbook and return a list of objects containing sheet data.
    Handles merged cells by getting values from the top-left cell of merged ranges.

    Args:
        workbook: An openpyxl workbook object

    Returns:
        list: List of objects containing sheet name and row-wise data
    """
    result = []

    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        data = []

        # Get headers from first row
        headers = [cell.value for cell in sheet[1]]

        # Get data from remaining rows
        for row in sheet.iter_rows(min_row=2):
            row_data = {}
            for header, cell in zip(headers, row):
                # Check if the cell is part of a merged range
                is_merged = False
                for merged_range in sheet.merged_cells.ranges:
                    if cell.coordinate in merged_range:
                        is_merged = True
                        # Get the value from the top-left cell of the merged range
                        top_left_cell = sheet.cell(row=merged_range.min_row, column=merged_range.min_col)
                        row_data[header] = top_left_cell.value
                        break
                if not is_merged:
                    row_data[header] = cell.value
            data.append(row_data)

        result.append({"sheetname": sheet_name, "data": data})

    return result
