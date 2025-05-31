from textwrap import dedent

OUTPUT_FORMAT = {
    "fill": {
        "type": "color",
        "value": "#001141"
    },
    "elements": [
        {
            "type": "image",
            "top": 0,
            "left": 0,
            "width": 249,
            "height": 405,
            "rotate": 0,
            "src": "data:image/png;base64,....",
            "opacity": 1,
            "isFlipV": False,
            "isFlipH": False,
            "order": 1676,
            "rect": {
                "t": 15.874,
                "b": 2.591,
                "l": 0,
                "r": 24.578
            },
            "geom": "rect",
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0"
        },
        {
            "left": 275,
            "top": 36,
            "width": 402,
            "height": 26,
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: left;\"><span style=\"color: #0F62FE;font-size: 22pt;font-family: Open Sans;font-weight: bold;\">Today's Agenda</span></p>",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "mid",
            "name": "Google Shape;54;p15",
            "order": 1722,
            "type": "text",
            "isVertical": False
        },
        {
            "left": 275,
            "top": 148,
            "width": 20,
            "height": 20,
            "borderColor": "#666666",
            "borderWidth": 0.75,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: center;\"><span style=\"color: #1754F1;font-size: 12pt;font-family: Lexend;\">2</span></p>",
            "isFlipV": False,
            "isFlipH": True,
            "rotate": 0,
            "vAlign": "mid",
            "name": "Google Shape;55;p15",
            "order": 1773,
            "type": "shape",
            "shapType": "ellipse",
            "cornerRadius": {
                "topLeft": 0,
                "topRight": 0,
                "bottomRight": 0,
                "bottomLeft": 0
            }
        },
        {
            "left": 306,
            "top": 148,
            "width": 371,
            "height": 19,
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: left;\"><span style=\"color: #121619;font-size: 16pt;font-family: Open Sans;font-weight: bold;\">State of Loyalty Program in Retail 2024</span></p>",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "up",
            "name": "Google Shape;56;p15",
            "order": 1819,
            "type": "text",
            "isVertical": False
        },
        {
            "left": 275,
            "top": 198,
            "width": 20,
            "height": 20,
            "borderColor": "#666666",
            "borderWidth": 0.75,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: center;\"><span style=\"color: #1754F1;font-size: 12pt;font-family: Lexend;\">3</span></p>",
            "isFlipV": False,
            "isFlipH": True,
            "rotate": 0,
            "vAlign": "mid",
            "name": "Google Shape;57;p15",
            "order": 1870,
            "type": "shape",
            "shapType": "ellipse",
            "cornerRadius": {
                "topLeft": 0,
                "topRight": 0,
                "bottomRight": 0,
                "bottomLeft": 0
            }
        },
        {
            "left": 308,
            "top": 199,
            "width": 391,
            "height": 19,
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: left;\"><span style=\"color: #121619;font-size: 16pt;font-family: Open Sans;font-weight: bold;\">Loyalty Program Examples & Recommendation</span></p>",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "up",
            "name": "Google Shape;58;p15",
            "order": 1916,
            "type": "text",
            "isVertical": False
        },
        {
            "left": 275,
            "top": 100,
            "width": 20,
            "height": 20,
            "borderColor": "#666666",
            "borderWidth": 0.75,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: center;\"><span style=\"color: #1754F1;font-size: 12pt;font-family: Lexend;\">1</span></p>",
            "isFlipV": False,
            "isFlipH": True,
            "rotate": 0,
            "vAlign": "mid",
            "name": "Google Shape;59;p15",
            "order": 1967,
            "type": "shape",
            "shapType": "ellipse",
            "cornerRadius": {
                "topLeft": 0,
                "topRight": 0,
                "bottomRight": 0,
                "bottomLeft": 0
            }
        },
        {
            "left": 306,
            "top": 100,
            "width": 371,
            "height": 19,
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: left;\"><span style=\"color: #121619;font-size: 16pt;font-family: Open Sans;font-weight: bold;\">About Xeno</span></p>",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "up",
            "name": "Google Shape;60;p15",
            "order": 2013,
            "type": "text",
            "isVertical": False
        },
        {
            "left": 308,
            "top": 247,
            "width": 391,
            "height": 19,
            "borderColor": "#000000",
            "borderWidth": 0,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": "",
            "content": "<p style=\"text-align: left;\"><span style=\"color: #121619;font-size: 16pt;font-family: Open Sans;font-weight: bold;\">Email Marketing</span></p>",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "up",
            "name": "Google Shape;62;p15",
            "order": 2110,
            "type": "text",
            "isVertical": False
        }
    ],
    "layoutElements": [
        {
            "left": 0,
            "top": -0.01,
            "width": 720,
            "height": 405,
            "borderColor": "#595959",
            "borderWidth": 0.75,
            "borderType": "solid",
            "borderStrokeDasharray": "0",
            "fill": {
                "type": "color",
                "value": "#FFFFFF"
            },
            "content": "",
            "isFlipV": False,
            "isFlipH": False,
            "rotate": 0,
            "vAlign": "mid",
            "name": "Google Shape;387;p100",
            "order": 886,
            "type": "shape",
            "shapType": "rect"
        }
    ],
    "note": ""
}

SLIDE_SELECTION_PROMPT = dedent(
f"""
    You are a senior pre sales engineer for Xeno. who is expert at making slides and designing slides for clients.

    **user Input**
     ##Transcript Summary: You will be provided with a summary of meeting which have happened with Xeno Client.
     ##Slides Outline: One of the outline from the many generated Outline for we you have to find a best matching Slide

    **Tools**
     ## SolutionDeckSearchTool: 
                a. which can provide you slides from master deck. 
                b. previous case studies slides
                c. Slides from previous clients
                d. product realted Slides.
                etc.....
    
    **Objective**:
        1. Understand the Tanscript summary.
        2. Go though slide Outline.
        3. Search using Tool to find the best match.
            a. Try to search nature of slide and context of slide for better retrival.
        4. Identify which slide suites the outline better and select that slide.
        5. You are allowed to select multiple slides for a single outline.
        6. Also there is a possiblity you say that we don't have any match for the slide.

    **Additional Notes**: 
        1. The output of this will be used to render on the frontend canvas. 
        2. You are allowed to change any quick content which is according to our slide while generating results.
        3. Only Focus on changing text.


    *****This output is just an example. Number of elements will change in output.
    
    ✅ **Output Format Example**  
    List[{OUTPUT_FORMAT}]
    
    Or an empty list:  
    ```json
    []
    ```

    Begin now.
    """
)
