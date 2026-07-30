// src/App.tsx — ルーティングと ⌘K のグローバルハンドラ。

import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import { Launcher } from './components/Launcher'
import { Shell } from './components/Shell'
import { HelpPage } from './pages/Help'
import { LibraryPage } from './pages/Library'
import { RunDetailPage } from './pages/RunDetail'
import { RunsPage } from './pages/Runs'
import { SchedulesPage } from './pages/Schedules'
import { SettingsPage } from './pages/Settings'
import { TaskDetailPage } from './pages/TaskDetail'
import { TasksPage } from './pages/Tasks'

export function App() {
  const [launcherOpen, setLauncherOpen] = useState(false)

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setLauncherOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      <Shell>
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/tasks" element={<TasksPage onOpenLauncher={() => setLauncherOpen(true)} />} />
          {/* :taskId より前に置く。後ろだと "new" がIDとして解釈される */}
          <Route path="/tasks/new" element={<TaskDetailPage isNew />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:execId" element={<RunDetailPage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/settings" element={<Navigate to="/settings/models" replace />} />
          <Route path="/settings/:section" element={<SettingsPage />} />
          <Route path="/help" element={<Navigate to="/help/podman" replace />} />
          <Route path="/help/:topic" element={<HelpPage />} />
          <Route path="*" element={<Navigate to="/tasks" replace />} />
        </Routes>
      </Shell>
      <Launcher open={launcherOpen} onClose={() => setLauncherOpen(false)} />
    </>
  )
}
