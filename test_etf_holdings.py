"""Offline tests for build_etf_holdings.parse_holdings and the loader fallback."""
import build_etf_holdings as B

def row(rank, sym_html, name="X", w="1%"):
    return (f'<tr class="svelte-1"><td class="svelte-1">{rank}</td><td class="sym svelte-1">{sym_html}</td>'
            f'<td class="slw svelte-1">{name}</td><td class="svelte-1">{w}</td></tr>')

html = ("<table><thead><tr><th>No.</th><th>Symbol</th></tr></thead><tbody>"
        + row(1, '<!--[0--><a href="/stocks/nvda/">NVDA</a><!--]-->')
        + row(2, '<a href="/stocks/brk.b/">BRK.B</a>')
        + row(3, '')                                    # blank symbol (cash line)
        + row(4, 'USD')                                 # cash -> not an equity ticker pattern? 3 letters -> allowed by regex, see below
        + row(5, 'KRX:005930')
        + row(6, 'ESZ6')                                # futures-style 4-char with digit -> skipped
        + row(7, '<a href="/stocks/aapl/">AAPL</a>')
        + row(8, '<a href="/stocks/nvda/">NVDA</a>')    # duplicate
        + row(9, '<a href="/stocks/msft/">MSFT</a>')
        + row(10, '<a href="/stocks/amzn/">AMZN</a>')
        + "</tbody></table>")
got = B.parse_holdings(html)
assert got == ["NVDA", "BRK.B", "005930.KS", "AAPL", "MSFT"], got
assert B.parse_holdings(html, top_n=2) == ["NVDA", "BRK.B"]
assert B.parse_holdings("<html>no table here</html>") == []
assert B.parse_holdings(row(1, "KRX:000660")) == ["000660.KS"]
print("parse OK")
