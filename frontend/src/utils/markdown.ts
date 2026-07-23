/**
 * 轻量 Markdown → HTML 渲染器（M3.09 ReportViewer）。
 *
 * 不引入外部依赖，只处理 M3.05 ReportGenerator 输出的 5 段式报告会用到的
 * 语法：H2 标题、加粗、无序列表、表格、段落。
 * 所有 HTML 特殊字符在最终输出前转义，防止 XSS。
 */

function escapeHtml(input: string): string {
  return input
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderInline(text: string): string {
  return escapeHtml(text)
    // 加粗 **text**
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // 斜体 *text*（不跨加粗边界）
    .replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>')
}

function renderTable(lines: string[]): string {
  if (lines.length < 2) return `<p>${renderInline(lines.join('<br>'))}</p>`

  const headerCells = parseTableRow(lines[0])
  const bodyLines = lines.slice(2) // 跳过分隔行 |---|---|

  const th = headerCells.map((cell) => `<th>${renderInline(cell)}</th>`).join('')
  const thead = `<thead><tr>${th}</tr></thead>`

  const tbodyRows = bodyLines
    .map((line) => {
      const cells = parseTableRow(line)
      const td = cells.map((cell) => `<td>${renderInline(cell)}</td>`).join('')
      return `<tr>${td}</tr>`
    })
    .join('')
  const tbody = tbodyRows ? `<tbody>${tbodyRows}</tbody>` : ''

  return `<table class="fin-md-table">${thead}${tbody}</table>`
}

function parseTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

/**
 * 把 Markdown 文本渲染为 HTML 字符串。
 *
 * @param md Markdown 原文
 * @returns 可安全注入 v-html 的 HTML 字符串
 */
export function renderMarkdown(md: string): string {
  if (!md) return ''

  const lines = md.split('\n')
  const blocks: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    // 空行
    if (trimmed === '') {
      i++
      continue
    }

    // H2 标题
    if (trimmed.startsWith('## ')) {
      const content = trimmed.slice(3).trim()
      blocks.push(`<h2 class="fin-md-h2">${renderInline(content)}</h2>`)
      i++
      continue
    }

    // H3 标题
    if (trimmed.startsWith('### ')) {
      const content = trimmed.slice(4).trim()
      blocks.push(`<h3 class="fin-md-h3">${renderInline(content)}</h3>`)
      i++
      continue
    }

    // 无序列表
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      const listItems: string[] = []
      while (i < lines.length) {
        const l = lines[i].trim()
        if (l.startsWith('- ') || l.startsWith('* ')) {
          listItems.push(`<li>${renderInline(l.slice(2).trim())}</li>`)
          i++
        } else if (l === '') {
          i++
        } else {
          break
        }
      }
      blocks.push(`<ul class="fin-md-ul">${listItems.join('')}</ul>`)
      continue
    }

    // 表格
    if (trimmed.startsWith('|')) {
      const tableLines: string[] = [trimmed]
      i++
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i].trim())
        i++
      }
      // 至少需要表头 + 分隔行
      if (tableLines.length >= 2) {
        blocks.push(renderTable(tableLines))
      } else {
        blocks.push(`<p>${renderInline(tableLines.join(' '))}</p>`)
      }
      continue
    }

    // 普通段落（支持软换行）
    const paragraphLines: string[] = []
    while (i < lines.length && lines[i].trim() !== '') {
      paragraphLines.push(lines[i].trim())
      i++
    }
    const content = paragraphLines.join(' ')
    blocks.push(`<p>${renderInline(content)}</p>`)
  }

  return `<div class="fin-md">${blocks.join('')}</div>`
}
