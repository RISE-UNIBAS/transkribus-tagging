"""
Transkribus API Client
-----------------------
Client for interacting with the Transkribus Legacy API using session-based authentication.
This client fetches additional document and page metadata that is not available in
the PageXML export, such as labels, tags, and excluded status.
"""

import logging
import requests
import xml.etree.ElementTree as ET
import xmltodict
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class TranskribusAPIClient:
    """Client for interacting with the Transkribus Legacy API."""

    AUTH_URL = "https://transkribus.eu/TrpServer/rest/auth/login"
    BASE_URL = "https://transkribus.eu/TrpServer/rest"

    def __init__(self, username: str, password: str):
        """
        Initialize the Transkribus API client.

        Args:
            username: Transkribus username
            password: Transkribus password
        """
        self.username = username
        self.password = password
        self.session_id: Optional[str] = None

    def authenticate(self) -> bool:
        """
        Authenticate with Transkribus and obtain session ID.

        Returns:
            True if authentication successful, False otherwise
        """
        payload = {"user": self.username, "pw": self.password}

        try:
            response = requests.post(self.AUTH_URL, data=payload, timeout=30)

            if response.status_code != 200:
                logger.error(
                    f"Authentication failed with status {response.status_code}"
                )
                logger.error(f"Response: {response.text[:500]}")
                return False

            # Parse XML response
            xml_response = xmltodict.parse(response.text)
            self.session_id = xml_response.get("trpUserLogin", {}).get("sessionId")

            if not self.session_id:
                logger.error("No session ID in authentication response")
                return False

            logger.info("Successfully authenticated with Transkribus API")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to authenticate with Transkribus API: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error parsing authentication response: {e}")
            return False

    def _get_cookies(self) -> Dict[str, str]:
        """
        Get cookies with session ID.

        Returns:
            Dictionary of cookies

        Raises:
            ValueError: If not authenticated
        """
        if not self.session_id:
            raise ValueError("Not authenticated. Call authenticate() first.")
        return {"JSESSIONID": self.session_id}

    def get_full_document(
        self, collection_id: int, document_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get complete document information including all metadata.

        Args:
            collection_id: Collection ID
            document_id: Document ID

        Returns:
            Full document data or None if request fails
        """
        url = f"{self.BASE_URL}/collections/{collection_id}/{document_id}/fulldoc"

        try:
            response = requests.get(url, cookies=self._get_cookies(), timeout=30)
            response.raise_for_status()
            logger.debug(
                f"Successfully fetched full document {document_id} from collection {collection_id}"
            )
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get full document {document_id}: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def extract_document_labels(self, doc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract label and metadata information from document data.

        Args:
            doc_data: Document data from API

        Returns:
            Dictionary with extracted metadata including:
            - labels: List of document-level labels
            - page_labels_available: List of available page label types
            - pages: Dictionary mapping page numbers to their labels
        """
        result = {"labels": [], "page_labels_available": [], "pages": {}}

        # Extract document-level labels
        if "md" in doc_data and "labels" in doc_data["md"]:
            result["labels"] = doc_data["md"]["labels"]

        # Extract available page label types
        if "md" in doc_data and "pageLabels" in doc_data["md"]:
            result["page_labels_available"] = doc_data["md"]["pageLabels"]

        # Extract page-specific labels
        if "pageList" in doc_data and "pages" in doc_data["pageList"]:
            for page in doc_data["pageList"]["pages"]:
                page_nr = page.get("pageNr")
                page_id = page.get("pageId")
                page_labels = page.get("labels", [])

                if page_id:
                    result["pages"][str(page_id)] = {
                        "page_nr": page_nr,
                        "labels": page_labels,
                        "is_excluded": any(
                            label.get("name", "").lower() == "exclude"
                            for label in page_labels
                        ),
                    }

        return result

    def upload_page_transcript(
        self,
        collection_id: int,
        doc_id: int,
        page_nr: int,
        xml_content: str,
        status: str = "IN_PROGRESS",
        note: str = "Updated via TWF",
    ) -> bool:
        """
        Upload a modified PAGE XML transcript to Transkribus.

        Endpoint: POST /collections/{collId}/{docId}/{pageNr}/text
        Sends the raw XML as the request body with Content-Type: application/xml.
        Returns True on success, False on failure.
        """
        url = f"{self.BASE_URL}/collections/{collection_id}/{doc_id}/{page_nr}/text"
        params = {"status": status, "note": note, "overwrite": "true"}
        headers = {"Content-Type": "application/xml"}
        try:
            response = requests.post(
                url,
                params=params,
                headers=headers,
                data=xml_content.encode("utf-8"),
                cookies=self._get_cookies(),
                timeout=60,
            )
            if response.status_code == 200:
                logger.info(
                    f"Successfully uploaded PAGE XML for page {page_nr} "
                    f"(doc {doc_id}, collection {collection_id})"
                )
                return True
            logger.error(
                f"Upload failed {response.status_code}: {response.text}"
            )
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Error uploading transcript: {e}")
            return False

    def get_page_status(self, page_xml: str) -> Optional[str]:
        """
        Extract the page processing status from a PAGE XML string.

        Reads the status attribute from the <TranskribusMetadata> element,
        which Transkribus uses to track workflow state (e.g. "IN_PROGRESS", "DONE", "GT").

        Args:
            page_xml: PAGE XML string as returned by get_page_xml()

        Returns:
            Status string, or None if the element/attribute is not found
        """
        PAGE_NS = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"
        try:
            root = ET.fromstring(page_xml)
            # Try with the standard PAGE XML namespace first
            metadata = root.find(f".//{{{PAGE_NS}}}TranskribusMetadata")
            if metadata is None:
                # Fall back to no-namespace search (some exports omit the namespace)
                metadata = root.find(".//TranskribusMetadata")
            if metadata is not None:
                return metadata.get("status")
            logger.warning("TranskribusMetadata element not found in PAGE XML")
            return None
        except ET.ParseError as e:
            logger.error(f"Failed to parse PAGE XML when reading status: {e}")
            return None

    def get_page_xml(
        self, collection_id: int, doc_id: int, page_nr: int
    ) -> Optional[str]:
        """
        Download the latest PAGE XML transcript for a given page.

        Fetches the transcript list for the page, then downloads the XML
        from the URL of the most recent transcript.

        Args:
            collection_id: Collection ID
            doc_id: Document ID
            page_nr: Page number (1-based)

        Returns:
            PAGE XML string, or None if the request fails
        """
        list_url = f"{self.BASE_URL}/collections/{collection_id}/{doc_id}/{page_nr}/list"
        try:
            response = requests.get(
                list_url, cookies=self._get_cookies(), timeout=30
            )
            response.raise_for_status()
            transcripts = response.json()
            if not transcripts:
                logger.error(f"No transcripts found for page {page_nr}")
                return None
            xml_url = transcripts[0].get("url")
            if not xml_url:
                logger.error("Transcript entry has no URL")
                return None
            xml_response = requests.get(xml_url, timeout=30)
            xml_response.raise_for_status()
            logger.info(
                f"Downloaded PAGE XML for page {page_nr} "
                f"(doc {doc_id}, collection {collection_id})"
            )
            return xml_response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get PAGE XML for page {page_nr}: {e}")
            return None

    def enrich_document_metadata(
        self, collection_id: int, document_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch full document data and extract relevant metadata.

        Args:
            collection_id: Collection ID
            document_id: Document ID

        Returns:
            Enriched metadata dictionary or None if request fails
        """
        doc_data = self.get_full_document(collection_id, document_id)
        if not doc_data:
            return None

        return self.extract_document_labels(doc_data)
