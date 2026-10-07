import { ChatWidget } from './components/ChatWidget'
import { Dashboard } from './components/Dashboard'

export default function App() {
  return (
    <div className="min-h-full">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">Order desk</h1>
            <p className="text-xs text-slate-500">Demo Distributor · incoming purchase orders, checked against the ERP</p>
          </div>
        </div>
      </header>
      <Dashboard />
      <ChatWidget />
    </div>
  )
}
