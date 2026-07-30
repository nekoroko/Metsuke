// src/components/SplitPane.tsx — 左右に分けて、境界をドラッグで動かせる枠。
//
// 幅は localStorage に残す。実行を開くたびに毎回動かし直すのは手間なので。
//
// キーボードでも動かせるようにしてある（← → で 24px、Home で既定に戻す）。
// マウス専用にすると、フォーカスがこの境界に来たときに何もできなくなる。

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

const MIN = 280          // これ以上狭めると中身が読めない
const STEP = 24

interface Props {
  storageKey: string
  defaultLeft: number
  left: ReactNode
  right: ReactNode
}

export function SplitPane({ storageKey, defaultLeft, left, right }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem(storageKey))
    return Number.isFinite(saved) && saved >= MIN ? saved : defaultLeft
  })
  const [dragging, setDragging] = useState(false)

  const clamp = useCallback((next: number) => {
    const total = containerRef.current?.clientWidth ?? 0
    // 右側にも最低幅を残す。片方を潰せてしまうと戻せなくなる
    const max = Math.max(MIN, total - MIN)
    return Math.min(max, Math.max(MIN, next))
  }, [])

  const apply = useCallback((next: number) => {
    const value = clamp(next)
    setWidth(value)
    window.localStorage.setItem(storageKey, String(value))
  }, [clamp, storageKey])

  useEffect(() => {
    if (!dragging) return
    function onMove(e: MouseEvent) {
      const rect = containerRef.current?.getBoundingClientRect()
      if (!rect) return
      apply(e.clientX - rect.left)
    }
    function onUp() {
      setDragging(false)
    }
    // ドラッグ中は選択が走ってテキストが青くなるので止める
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [dragging, apply])

  // 窓を狭めたときに、片側がはみ出したままにならないようにする
  useEffect(() => {
    function onResize() {
      setWidth((w) => clamp(w))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [clamp])

  return (
    <div className="split" ref={containerRef}>
      <div className="split-left" style={{ width }}>
        {left}
      </div>
      <div
        className={`split-bar${dragging ? ' on' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="左右の幅を変える"
        tabIndex={0}
        onMouseDown={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDoubleClick={() => apply(defaultLeft)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft') {
            e.preventDefault()
            apply(width - STEP)
          } else if (e.key === 'ArrowRight') {
            e.preventDefault()
            apply(width + STEP)
          } else if (e.key === 'Home') {
            e.preventDefault()
            apply(defaultLeft)
          }
        }}
      />
      <div className="split-right">{right}</div>
    </div>
  )
}
