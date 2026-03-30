"""
This script runs the entire pipeline:
    1.) Import necessary modules
    2.) Load environment variables (passwords, secrets, API keys, etc.) from a .env file
    3.) Read the prompt from a file
    4.) Create an instance of the Transkribus API client using the loaded credentials
    5.) Create an instance of the LLM client using the loaded API key
    6.) Loop through the specified page range:
        6.0.) Check if the page is in the list of excluded pages and skip it if so
        6.1.) Get the page content from Transkribus
        6.2.) Get TranskribusMetadata status and only progress if the page is IN_PROGRESS
        6.3.) Construct the input for the LLM by combining the prompt with the page content
        6.4.) Assess the response:
            6.4.1.) Check if the response text is a valid XML string or markdown-gated XML string.
            6.4.2.) Extract all tags from the custom attributes of TextLine elements to create a report of the tags found.
        6.5.) Send the response back to Transkribus to update the page XML with the new tags.
        6.6.) Update log and repeat for the next page
"""

##############################
# 1.) Import necessary modules
import logging                      # for logging messages from the API clients
import os                           # operating system functions
import re                           # for regular expressions (used to strip markdown fences)
import xml.etree.ElementTree as ET  # for parsing XML responses
from collections import Counter     # for counting entity tags in the report
from dotenv import load_dotenv      # for loading environment variables from a .env file
                                    # for interacting with the Transkribus API
from transkribus_api_client import TranskribusAPIClient
                                    # for creating an AI client to interact with LLM providers
from ai_client import create_ai_client

logging.basicConfig(level=logging.WARNING)  # suppress verbose API client debug messages

##############################
# 2.) Load environment variables (passwords, secrets, API keys, etc.) from a .env file
load_dotenv()                       # Load environment variables from .env file
API_KEY = os.getenv("API_KEY")      # Get the API key from environment variables
                                    # Get the Transkribus username from environment variables
TRANSKRIBUS_USERNAME = os.getenv("TRANSKRIBUS_USERNAME")
                                    # Get the Transkribus password from environment variables
TRANSKRIBUS_PASSWORD = os.getenv("TRANSKRIBUS_PASSWORD")

                                    # Collection and document IDs for Transkribus
TRANSKRIBUS_COLLECTION_ID  = 2205373
TRANSKRIBUS_DOCUMENT_ID    = 11509276
TRANSKRIBUS_PAGE_RANGE     = (5,27)
TRANSKRIBUS_EXCLUDED_PAGES = []     # Only pages with status IN_PROGRESS are processed.
                                    # This lets you exclude pages that are not ready for processing
                                    # but still have the IN_PROGRESS status.

##############################
# 3.) Read the prompt from a file
with open("prompt.txt", "r", encoding="utf8") as f:
    prompt = f.read()

##############################
# 4.) Create an instance of the Transkribus API client using the loaded credentials
transkribus = TranskribusAPIClient(TRANSKRIBUS_USERNAME, TRANSKRIBUS_PASSWORD)
assert transkribus.authenticate(), "Transkribus authentication failed — check TRANSKRIBUS_USERNAME and TRANSKRIBUS_PASSWORD in your .env file"
print("Authenticated with Transkribus")

##############################
# 5.) Create an instance of the LLM client using the loaded API key
llm = create_ai_client('google', api_key=API_KEY)
llm_model = "gemini-3.1-pro-preview"        # Specify the LLM model to use

###############################
# 6.) Loop through the specified page range
for page_num in range(TRANSKRIBUS_PAGE_RANGE[0], TRANSKRIBUS_PAGE_RANGE[1]):
    # 6.0.) Check if the page is in the list of excluded pages and skip it if so
    if page_num in TRANSKRIBUS_EXCLUDED_PAGES:
        print(f"Skipping excluded page {page_num}")
        continue

    print("Processing page", page_num)
    # 6.1.) Get the page content from Transkribus
    page_xml = transkribus.get_page_xml(TRANSKRIBUS_COLLECTION_ID,
                                        TRANSKRIBUS_DOCUMENT_ID,
                                        page_num)

    # 6.2.) Get TranskribusMetadata status and only progress if the page is IN_PROGRESS
    page_status = transkribus.get_page_status(page_xml)
    if page_status != "IN_PROGRESS":
        print(f"Skipping page {page_num} with status {page_status}")
        continue

    # 6.3.) Construct the input for the LLM by combining the prompt with the page content
    constructed_prompt = f"The following PAGE-XML is the context for this request:\n{page_xml}\n\n{prompt}"
    response = llm.prompt(llm_model, constructed_prompt)

    # 6.4.) Assess the response
    # 6.4.1.) Check if the response text is a valid XML string or markdown-gated XML string.
    checked_xml = response.text.strip()
    # Strip markdown code fences if the LLM wrapped the XML (e.g. ```xml ... ```)
    if checked_xml.startswith("```"):
        checked_xml = re.sub(r'^```[a-z]*\s*', '', checked_xml)
        checked_xml = re.sub(r'\s*```$', '', checked_xml).strip()
    # Validate that the result is well-formed XML before uploading
    try:
        xml_root = ET.fromstring(checked_xml)
    except ET.ParseError as e:
        print(f"Page {page_num}: LLM returned invalid XML ({e}) — skipping upload")
        continue

    # 6.4.2.) Extract all tags from the custom attributes of TextLine elements to create a report of the tags found.
    # The Transkribus custom attribute uses the entity type directly as the tag name, e.g.:
    #   custom="readingOrder {index:3;} person {offset:0;length:12;} place {offset:0;length:19;}"
    ENTITY_TYPES = {"person", "place", "organisation", "date"}
    tag_counts = Counter()
    for elem in xml_root.iter():
        custom_attr = elem.get("custom", "")
        if not custom_attr:
            continue
        for match in re.finditer(r'(\w+)\s*\{', custom_attr):
            if match.group(1) in ENTITY_TYPES:
                tag_counts[match.group(1)] += 1
    if tag_counts:
        upload_note = "Tagged: " + ", ".join(f"{count}x {tag}" for tag, count in sorted(tag_counts.items()))
    else:
        upload_note = "Tagged by LLM"

    # 6.5.) Send the response back to Transkribus to update the page XML with the new tags.
    success = transkribus.upload_page_transcript(
        collection_id=TRANSKRIBUS_COLLECTION_ID,
        doc_id=TRANSKRIBUS_DOCUMENT_ID,
        page_nr=page_num,
        xml_content=checked_xml,
        status="DONE",
        note=upload_note,
    )
    if success:
        print(f"Successfully updated page {page_num}")
    else:
        print(f"Failed to update page {page_num}")

    # 6.6.) Update log and repeat for the next page
    print(f"Page {page_num}: {upload_note}")
