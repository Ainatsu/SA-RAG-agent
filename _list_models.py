from fastembed import TextEmbedding

for m in TextEmbedding.list_supported_models():
    name = m["model"]
    low = name.lower()
    if "multilingual" in low or "m3" in low or "multi" in low:
        size = m.get("size_in_GB", "?")
        print(f'{name}  dim={m["dim"]}  size={size}GB')
