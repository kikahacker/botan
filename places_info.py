import httpx

# ⚠️ ВСТАВЬ СЮДА ТОЛЬКО СВОЮ КУКУ ОТ СВОЕГО АККАУНТА
ROBLOSECURITY = "CAEaAhACIhwKBGR1aWQSFDExNTA3NjAxMzU5NzI4ODg4MjQxKAM._WHJHOV-F7CutZBKCUtupDB-ESBjwVRJLttaEHQi1flU7plf8Kie9FZ8EKCE7h4rs5c4b3s1Mys-LABuCmubp_n21WUB4lWg4V3WGOVXF0UkJRwCLU4zAd551AWwafCkzVb3GhO6iuw958inCgZN3tdX83i8g11292PixOZdTdhAkB7diqpeHiGoUkN3BmoRmOejxU8eYJIi4Qv7BSGv_QTgtSIhm-ICeiTJ4ul37_forvjCvFx_00778sBD14HzBjOG04YJmDBeS8AJxgFPB7Tl2YgkuPUlVlqzBtxSl6jYI1ibXqNEckuiubLszX_FxUhvTNIyaYj7yycj4sUBC_OAiRqzIDGKgk1WIkkOm94Jxz5rLuhBQi2x8nTNGCCV_eqPpFFpgUxco-KU_omh8USX-L9uuNYMoJZgBDQ8qg9YLKuCzbPVTYbmE-4fCnHnU_tQEgmbKrdPj7Kjrzva08cDsO_Bs58JtLAc4Mntc9EwT8ZhqfpauMyu1sq1HUoS6zdkuX2-hcCvdnkWBwJ0OQS3t9jrTFFsalYezDYB2Xuq-Q-Mf1hIpQzxOENSKyFUs4ELVr-vPeU8fRHp8iDa_8vJfY5sKigP8Fn9ri-KeJ8y4K4MJ7S0RFIy5MZlwQayTbqeIZOWhsspL-S0s4ebjYkB49QG-x5dvdcUY3ChP765TwkMaN5oLWPoWdHTQ1qV6-3ycegB0RZHwYsbhpE6roxaWMryrH5jNI-tFuGA5j1-H4U7ZGPUUWikL8Z4pZj4nDsCKeFniv_CymI_ay1_HmnrK98AIRKmQOMNShuaVMM9MSE9fbFaFJ13E8q26wbh5is8VcF2qAS2yaxoC1oLCVwMuxQkiwNWhVewfq5SSO0wW8fQHWp4hKP6Ya3Zo9iFjdcwTJ0T4uv9PFMOmYVCL1SbtchL6mJJzuWtCXoEscjzJ2x5A9HEW422fR3Eqnknc1Cd4yDbE8bwpr0KuJrGdIkCnuGRGyuTZrBrnv-RGXZVAJb9xwyWP_qQuMRYD4sI4X-H0fkKt09XtBW1N9ijXTonk3CxPivBgfZfFZyhhyBDvocGdlugJPLLyUBUjksgZ3uLFOJNJn2pKaDjA36rnvZ6ciljNM0lyHyTMGN7lE0StfbOZXEUb3OmaEIdpVxjUFHreg"
# ⚠️ И свой userId (тот, чей аккаунт открыт этой кукой)
USER_ID = 1119237244

import asyncio
import roblox_client as rbc
import re
import json

async def main():
    url = "https://apis.roblox.com/discovery-api/omni-recommendation"

    headers = {
        "Cookie": f".ROBLOSECURITY={ROBLOSECURITY}",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # sessionId по факту пофиг, берём любой строковый (браузер тоже генерит рандом)
    payload = {
        "pageType": "Home",
        "sessionId": "test-session-id-123",
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.post(url, headers=headers, json=payload)

    print("STATUS:", resp.status_code)
    if resp.status_code != 200:
        print("TEXT:", resp.text[:1000])
        return

    data = resp.json()

    # Просто посмотреть, какие есть sort'ы
    sorts = data.get("sorts") or data.get("data") or []
    print(f"Всего сортов: {len(sorts)}")

    for i, sort in enumerate(sorts):
        name = sort.get("name") or sort.get("displayName") or f"sort_{i}"
        items = sort.get("items") or sort.get("experiences") or []
        print(f"\n=== SORT #{i} — {name} (items: {len(items)}) ===")

        # покажем первые пару игр из каждого sorta
        for it in items[:3]:
            # в разных версиях поля могут называться по-разному
            uni_id = it.get("universeId") or it.get("universeid") or it.get("id")
            place_id = it.get("placeId") or it.get("placeid")
            title = it.get("name") or it.get("title") or ""
            extra = {k: v for k, v in it.items() if k.lower().endswith("time") or "play" in k.lower()}
            print(f"- title={title!r} uni={uni_id} place={place_id} extra={extra}")


if __name__ == "__main__":
    asyncio.run(main())