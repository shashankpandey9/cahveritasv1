import React from 'react'

/** Catches render errors so a bad payload shows a recoverable message instead
 *  of unmounting the whole app (the browser's blank white page). */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('CAH VERITAS render error:', error, info?.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="app appError">
        <div className="errorCard">
          <div className="errorTitle">Something went wrong rendering CAH VERITAS</div>
          <div className="errorText">{String(this.state.error?.message || this.state.error)}</div>
          <button className="btn primary" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    )
  }
}
