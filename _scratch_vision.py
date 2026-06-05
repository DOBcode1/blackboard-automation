from extractors import extract_text_via_vision

paths = [
    r"C:\Users\dcob7\OneDrive\Pictures\Screenshots\Operations Test screenshot.png",
]
for p in paths:
    try:
        data = open(p, "rb").read()
        text, method, conf = extract_text_via_vision(data, p)
        head = text[:300] if text else "(no text)"
        print(f"\n=== {p}\nmethod={method}  confidence={conf}\n{head}")
    except Exception as e:
        print(f"\n=== {p}\nERROR: {type(e).__name__}: {e}")
