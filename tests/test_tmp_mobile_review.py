import copy
import json

import pytest

from tests.test_front_pages import browser, fixtures, open_page  # noqa: F401

MEASURE = """() => [...document.querySelectorAll('article.match')].filter((c) => c.offsetParent).map((card) => {
  const r = (e) => { if (!e) return null; const b = e.getBoundingClientRect(); return {l: +b.left.toFixed(1), r: +b.right.toFixed(1), t: +b.top.toFixed(1), b: +b.bottom.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1)}; };
  const board = card.querySelector('.match__board');
  const score = card.querySelector('.score');
  const teams = [...card.querySelectorAll('.team')].map((t) => {
    const name = t.querySelector('.team__name');
    const full = t.querySelector('.team__full');
    const crest = t.querySelector('.crest');
    const lh = parseFloat(getComputedStyle(name).lineHeight);
    return { side: t.classList.contains('team--home') ? 'home' : 'away', text: full.textContent,
      fullDisplay: getComputedStyle(full).display, shortDisplay: getComputedStyle(t.querySelector('.team__short')).display,
      dir: getComputedStyle(t).flexDirection, team: r(t), name: r(name), full: r(full), crest: r(crest), crestTag: crest?.tagName,
      lines: +(full.getBoundingClientRect().height / lh).toFixed(2),
      overflowName: name.scrollWidth - name.clientWidth, overflowFull: full.scrollWidth - full.clientWidth,
      color: getComputedStyle(name).color };
  });
  return { cardW: card.getBoundingClientRect().width, winner: card.dataset.winner, status: card.dataset.status, card: r(card), board: r(board), score: r(score), teams };
})"""

NAMES = {
    1: "Belojardinense",
    2: "Internacional",
    3: "Caruaruense",
    4: "Ferroviário",
    5: "Independente",
    6: "Sociedade Esportiva Palmeiras do Nordeste Futebol Clube",
    7: "Íbis",
    8: "Americano",
}


def _rename(obj, names, crest=None):
    if isinstance(obj, dict):
        if "short_name" in obj and "color_primary" in obj and obj.get("id") in names:
            obj["name"] = names[obj["id"]]
            if crest is not None:
                obj["crest_url"] = crest
        for v in obj.values():
            _rename(v, names, crest)
    elif isinstance(obj, list):
        for v in obj:
            _rename(v, names, crest)


def _prep(names, crest=None):
    def prepare(api):
        api.fx = copy.deepcopy(api.fx)
        _rename(api.fx, names, crest)
    return prepare


def _problems(cards):
    out = []
    for c in cards:
        for t in c["teams"]:
            col_issue = t["name"]["l"] < t["team"]["l"] - 1 or t["name"]["r"] > t["team"]["r"] + 1
            score_overlap = c["score"] and (
                (t["side"] == "home" and t["name"]["r"] > c["score"]["l"] + 1)
                or (t["side"] == "away" and t["name"]["l"] < c["score"]["r"] - 1)
            )
            out_card = t["name"]["l"] < c["card"]["l"] or t["name"]["r"] > c["card"]["r"]
            if col_issue or score_overlap or out_card or t["overflowName"] > 1 or t["overflowFull"] > 1 or t["lines"] > 2.2:
                out.append({"text": t["text"], "cardW": c["cardW"], "team": t["team"], "name": t["name"], "score": c["score"], "lines": t["lines"], "ovN": t["overflowName"], "ovF": t["overflowFull"]})
    return out


@pytest.mark.parametrize("width", [320, 360, 390])
@pytest.mark.parametrize("url", ["/", "/competition.html?slug=pernambucano-raiz", "/competition.html?slug=copa-pernambuco&stage=6"])
@pytest.mark.parametrize("crest", [None, "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Crect width='10' height='10' fill='red'/%3E%3C/svg%3E"])
def test_tmp_long_names(open_page, url, width, crest):
    page, _, errors = open_page(url, width=width, prepare=_prep(NAMES, crest))
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(400)
    cards = page.evaluate(MEASURE)
    probs = _problems(cards)
    print("\nURL", url, width, "crest" if crest else "svg", "cards", len(cards))
    for p in probs:
        print("  PROBLEM", json.dumps(p, ensure_ascii=False))
    # crest alignment
    for c in cards:
        tops = [t["crest"]["t"] for t in c["teams"]]
        if abs(tops[0] - tops[1]) > 2:
            print("  CREST MISALIGN", c["cardW"], [(t["text"], t["crest"]["t"], t["lines"]) for t in c["teams"]])
    print("  scrollW ok:", page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
    print("  errors", errors)


@pytest.mark.parametrize("width", [1024, 1280])
def test_tmp_wide(open_page, width):
    page, _, errors = open_page("/", width=width, prepare=_prep(NAMES))
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(300)
    cards = page.evaluate(MEASURE)
    for c in cards:
        print("\n", c["cardW"], [(t["text"], t["dir"], t["fullDisplay"], t["shortDisplay"], t["lines"]) for t in c["teams"]])
    print("PROBS", _problems(cards))


@pytest.mark.parametrize("width", [320, 390])
def test_tmp_default_names(open_page, width):
    page, _, errors = open_page("/", width=width)
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(300)
    cards = page.evaluate(MEASURE)
    for c in cards:
        print("\nW", width, c["status"], c["winner"], [(t["text"], t["lines"], t["crest"]["t"], t["team"]["w"], t["name"]["w"], t["color"]) for t in c["teams"]])
    big = {1: "Belojardinense", 2: "Internacional"}
    page2, _, _ = open_page("/", width=width, prepare=_prep(big))
    page2.wait_for_selector("article.match .team__name")
    page2.wait_for_timeout(300)
    for c in page2.evaluate(MEASURE):
        for t in c["teams"]:
            if t["text"] in big.values():
                print("BIG", width, t["text"], "team w", t["team"]["w"], "name w", t["name"]["w"], "full", t["full"]["w"])


def test_tmp_shot320(open_page):
    big = {1: "Belojardinense", 2: "Internacional"}
    page, _, _ = open_page("/", width=320, prepare=_prep(big))
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(400)
    info = page.evaluate("""() => [...document.querySelectorAll('article.match .team__full')].slice(0, 4).map((f) => {
      const range = document.createRange(); range.selectNodeContents(f);
      const rects = [...range.getClientRects()].map((r) => [Math.round(r.left), Math.round(r.right), Math.round(r.top)]);
      const cs = getComputedStyle(f);
      return [f.textContent, f.getBoundingClientRect().width, f.scrollWidth, f.clientWidth, cs.fontSize, cs.fontFamily, rects, getComputedStyle(f.parentElement).fontSize, f.closest('.team').getBoundingClientRect().left, f.closest('.team').getBoundingClientRect().right];
    })""")
    for i in info:
        print("\nINFO", i)
    card = page.locator("article.match").first
    card.scroll_into_view_if_needed()
    card.screenshot(path="/tmp/claude-0/-home-user-futebol-de-raizes/534a0cd0-5ba7-54f9-a360-51e6590e5aad/scratchpad/card320.png")


@pytest.mark.parametrize("width", [320, 340, 360, 380, 390])
def test_tmp_debug_width(open_page, width):
    big = {1: "Belojardinense", 2: "Internacional"}
    page, _, _ = open_page("/", width=width, prepare=_prep(big))
    page.wait_for_selector("article.match .team__name")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(1500)
    info = page.evaluate("""() => [...document.querySelectorAll('article.match')].slice(0, 2).flatMap((card) => [...card.querySelectorAll('.team')].map((t) => {
      const n = t.querySelector('.team__name'), f = t.querySelector('.team__full');
      const cs = getComputedStyle(t), ns = getComputedStyle(n);
      return [f.textContent, card.getBoundingClientRect().width, t.getBoundingClientRect().width, cs.flexDirection, cs.alignItems, n.getBoundingClientRect().width, ns.width, ns.maxWidth, ns.fontSize, f.scrollWidth, f.clientWidth, t.querySelector('.crest').getBoundingClientRect().width];
    }))""")
    for i in info:
        print("\nDBG", width, i)


def test_tmp_debug_rules(open_page):
    page, _, _ = open_page("/", width=320)
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(800)
    info = page.evaluate("""() => {
      const t = document.querySelector('article.match .team--home');
      const f = t.querySelector('.team__full'), s = t.querySelector('.team__short'), n = t.querySelector('.team__name');
      const out = {full: getComputedStyle(f).display, short: getComputedStyle(s).display, dir: getComputedStyle(t).flexDirection,
        nameFs: getComputedStyle(n).fontSize, clamp: getComputedStyle(f).webkitLineClamp, ta: getComputedStyle(t).textAlign};
      const found = [];
      for (const sheet of document.styleSheets) {
        let rules; try { rules = sheet.cssRules; } catch { continue; }
        const walk = (list, path) => { for (const r of list) {
          if (r.cssRules) walk(r.cssRules, path + ' > ' + (r.conditionText || r.name || r.constructor.name));
          if (r.selectorText && /^\\.team(\\b|__full|__short|--home|--away)/.test(r.selectorText)) found.push(path + ' :: ' + r.cssText.slice(0, 140));
        } };
        walk(rules, sheet.href?.split('/').pop() || 'inline');
      }
      out.found = found;
      return out;
    }""")
    print("\nRULES", json.dumps(info, indent=1, ensure_ascii=False))
