from image_crawler.crawler import extract_page, sniff_image_type


def test_extracts_all_image_sources():
    html = """
    <html><head>
      <meta property="og:image" content="/og.jpg">
      <meta name="twitter:image" content="https://cdn.example.com/tw.png">
    </head><body>
      <img src="a.png">
      <img data-src="/lazy.webp" src="data:image/gif;base64,AAAA">
      <img srcset="small.jpg 320w, big.jpg 1280w, mid.jpg 640w">
      <picture><source srcset="pic@1x.avif 1x, pic@2x.avif 2x"></picture>
      <video><source src="clip.mp4"></video>
      <a href="/full/photo.JPG">full size</a>
      <a href="/gallery?page=2#top">next</a>
      <a href="mailto:me@example.com">mail</a>
    </body></html>
    """
    page = extract_page(html, "https://example.com/blog/post")
    images, links = page.images, page.links
    assert set(images) == {
        "https://example.com/og.jpg",
        "https://cdn.example.com/tw.png",
        "https://example.com/blog/a.png",
        "https://example.com/lazy.webp",
        "https://example.com/blog/big.jpg",
        "https://example.com/blog/pic@2x.avif",
        "https://example.com/full/photo.JPG",
    }
    assert links == ["https://example.com/gallery?page=2"]


def test_base_href_is_respected():
    images = extract_page('<base href="https://img.example.com/x/"><img src="y.png">', "https://example.com/").images
    assert images == ["https://img.example.com/x/y.png"]


def test_sniff_image_type():
    assert sniff_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_type(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"\0\0\0\x1cftypavif") == "image/avif"
    assert sniff_image_type(b'<?xml version="1.0"?><svg xmlns="...">') == "image/svg+xml"
    assert sniff_image_type(b"<html>") is None
