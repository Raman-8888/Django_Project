"""
policies.py — Redaction rules per document type.

Each policy defines:
  - extra_patterns : regex patterns unique to that doc type
  - llm_hint       : extra instructions sent to the LLM detector
  - required_fields: human-readable list used in prompts
"""

POLICIES = {
    "CV": {
        "required_fields": [
            "full name", "date of birth", "phone number", "email address",
            "home address", "LinkedIn URL", "GitHub URL", "nationality",
            "passport number", "driving licence number",
        ],
        "llm_hint": (
            "This is a CV/Resume. Focus on personal identifiers: "
            "name, DOB, phone, email, home address, social media URLs, "
            "nationality, passport/driving licence numbers."
        ),
    },
    "CONTRACT": {
        "required_fields": [
            "party names", "signatory names", "company registration numbers",
            "dates", "financial amounts", "bank account numbers",
            "addresses", "email addresses", "phone numbers",
        ],
        "llm_hint": (
            "This is a legal contract. Focus on: party names, signatory names, "
            "company registration numbers, contract dates, payment amounts, "
            "bank details, addresses, contact information."
        ),
    },
    "MEDICAL": {
        "required_fields": [
            "patient name", "date of birth", "patient ID", "diagnosis",
            "doctor name", "hospital name", "prescription details",
            "insurance number", "phone", "address",
        ],
        "llm_hint": (
            "This is a medical record. Focus on: patient name, DOB, patient ID, "
            "diagnosis codes, doctor/nurse names, hospital details, "
            "insurance numbers, prescriptions, contact details."
        ),
    },
    "COURT_FILING": {
        "required_fields": [
            "party names", "case number", "judge name", "lawyer names",
            "addresses", "dates", "witness names",
        ],
        "llm_hint": (
            "This is a court filing. Focus on: plaintiff/defendant names, "
            "case numbers, judge names, lawyer names, witness names, "
            "addresses, filing dates."
        ),
    },
    "FINANCIAL": {
        "required_fields": [
            "account numbers", "PAN number", "Aadhaar number",
            "company names", "director names", "financial amounts",
            "IFSC codes", "GST numbers",
        ],
        "llm_hint": (
            "This is a financial document. Focus on: account numbers, PAN, "
            "Aadhaar, GST numbers, IFSC codes, company names, director names, "
            "transaction amounts, dates."
        ),
    },
    "OTHER": {
        "required_fields": [
            "person names", "organisation names", "dates", "phone numbers",
            "email addresses", "addresses", "ID numbers",
        ],
        "llm_hint": (
            "Identify all personally identifiable information (PII) including: "
            "names, organisations, dates, phone numbers, emails, addresses, "
            "ID numbers, financial details."
        ),
    },
}

# Regex patterns that apply to ALL document types
UNIVERSAL_PATTERNS = {
    "PHONE_IN":    r"\b[6-9]\d{9}\b",                              # Indian mobile
    "PHONE_INTL":  r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,9}",
    "EMAIL":       r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    "AADHAAR":     r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
    "PAN":         r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
    "PASSPORT_IN": r"\b[A-Z][1-9]\d{7}\b",
    "URL":         r"https?://[^\s]+|www\.[^\s]+",
    "LINKEDIN":    r"linkedin\.com/in/[^\s]+",
    "GITHUB":      r"github\.com/[^\s]+",
}
