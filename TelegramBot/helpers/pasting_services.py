from httpx import AsyncClient
from bs4 import BeautifulSoup
from telegraph.aio import Telegraph

# The Telegraph API is served from both telegra.ph and graph.org, but a page
# created on one domain is NOT served by the other (the shared link 404s).
# graph.org is tried first because telegra.ph is blocked on many networks.
TELEGRAPH_DOMAINS = ("graph.org", "telegra.ph")


async def katbin_paste(text: str) -> str:
    """
    paste the text in katb.in website.
    """

    katbin_url = "https://katb.in"
    client = AsyncClient()
    response = await client.get(katbin_url)
    soup = BeautifulSoup(response.content, "html.parser")
    csrf_token = soup.find("input", {"name": "_csrf_token"}).get("value")
    try:
        paste_post = await client.post(
            katbin_url,
            data={"_csrf_token": csrf_token, "paste[content]": text},
            follow_redirects=False,
        )
        output_url = f"{katbin_url}{paste_post.headers['location']}"
        await client.aclose()
        return output_url
    except:
        return "something went wrong while pasting text in katb.in."


async def telegraph_paste(content: str, title="TelegramBot") -> str:
    """
    paste the text in telegra.ph (graph.org) website and return the page URL,
    falling back to the other Telegraph domain if page creation fails.
    """

    html_content = "<pre>" + content.replace("\n", "<br>") + "</pre>"

    for domain in TELEGRAPH_DOMAINS:
        telegraph = Telegraph(domain=domain)
        try:
            await telegraph.create_account(short_name=title)
            response = await telegraph.create_page(
                title=title, html_content=html_content)
            url = response["url"]
            try:
                await telegraph.revoke_access_token()
            except Exception:
                pass
            return url
        except Exception:
            continue

    # last resort: katb.in
    return await katbin_paste(content)


async def telegraph_image_paste(filepath: str) -> str:
    """
    paste the image in telegra.ph (graph.org) website and return a direct URL.
    The image is uploaded to the same domain that serves the file, otherwise
    the returned link would 404.
    """

    for domain in TELEGRAPH_DOMAINS:
        telegraph = Telegraph(domain=domain)
        try:
            image_url = await telegraph.upload_file(filepath)
            return f"https://{domain}/" + image_url[0]["src"]
        except Exception:
            continue

    return "something went wrong while posting image. "
