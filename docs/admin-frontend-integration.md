# Admin Frontend Integration Guide

This guide explains how to integrate the admin authentication system with your frontend application using Supabase.

## Overview

The admin system uses Supabase for authentication with a first-time password change requirement. Here's how it works:

1. **Super Admin Creation**: Admin is created with temporary password
2. **First Login**: Admin logs in with temporary password
3. **Password Change**: Admin is required to change password on first login
4. **Normal Operation**: Admin uses new password for subsequent logins

## Frontend Implementation

### 1. Supabase Client Setup

```javascript
import { createClient } from '@supabase/supabase-js'

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

export const supabase = createClient(supabaseUrl, supabaseAnonKey)
```

### 2. Admin Login Component

```javascript
import { useState } from 'react'
import { supabase } from '../lib/supabase'

export default function AdminLogin() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleLogin = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    try {
      const { data, error } = await supabase.auth.signInWithPassword({
        email,
        password
      })

      if (error) {
        setError(error.message)
        return
      }

      // Check if admin requires password change
      const response = await fetch('/api/admin/requires-password-change', {
        headers: {
          'Authorization': `Bearer ${data.session.access_token}`
        }
      })

      const { requires_password_change } = await response.json()

      if (requires_password_change) {
        // Redirect to password change page
        window.location.href = '/admin/change-password'
      } else {
        // Redirect to admin dashboard
        window.location.href = '/admin/dashboard'
      }

    } catch (err) {
      setError('Login failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleLogin}>
      <input
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="Email"
        required
      />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Password"
        required
      />
      <button type="submit" disabled={loading}>
        {loading ? 'Logging in...' : 'Login'}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}
```

### 3. Password Change Component

```javascript
import { useState } from 'react'
import { supabase } from '../lib/supabase'

export default function ChangePassword() {
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handlePasswordChange = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    if (newPassword !== confirmPassword) {
      setError('Passwords do not match')
      setLoading(false)
      return
    }

    try {
      // Get current session
      const { data: { session } } = await supabase.auth.getSession()
      
      if (!session) {
        setError('No active session')
        return
      }

      // Update password in Supabase
      const { error: updateError } = await supabase.auth.updateUser({
        password: newPassword
      })

      if (updateError) {
        setError(updateError.message)
        return
      }

      // Mark password as changed in backend
      const response = await fetch('/api/admin/mark-password-changed', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${session.access_token}`
        }
      })

      if (!response.ok) {
        setError('Failed to update password status')
        return
      }

      // Redirect to admin dashboard
      window.location.href = '/admin/dashboard'

    } catch (err) {
      setError('Password change failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2>Change Your Password</h2>
      <p>You are required to change your password on first login.</p>
      
      <form onSubmit={handlePasswordChange}>
        <input
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          placeholder="New Password"
          required
          minLength={8}
        />
        <input
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          placeholder="Confirm New Password"
          required
          minLength={8}
        />
        <button type="submit" disabled={loading}>
          {loading ? 'Changing Password...' : 'Change Password'}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  )
}
```

### 4. Protected Admin Route Component

```javascript
import { useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'

export default function ProtectedAdminRoute({ children }) {
  const [loading, setLoading] = useState(true)
  const [admin, setAdmin] = useState(null)

  useEffect(() => {
    checkAdmin()
  }, [])

  const checkAdmin = async () => {
    try {
      const { data: { session } } = await supabase.auth.getSession()
      
      if (!session) {
        window.location.href = '/admin/login'
        return
      }

      // Verify admin status with backend
      const response = await fetch('/api/admin/me', {
        headers: {
          'Authorization': `Bearer ${session.access_token}`
        }
      })

      if (!response.ok) {
        window.location.href = '/admin/login'
        return
      }

      const adminData = await response.json()
      setAdmin(adminData)

    } catch (error) {
      console.error('Auth check failed:', error)
      window.location.href = '/admin/login'
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return <div>Loading...</div>
  }

  if (!admin) {
    return null
  }

  return children
}
```

### 5. Admin Dashboard Component

```javascript
import { useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'

export default function AdminDashboard() {
  const [admin, setAdmin] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadAdminInfo()
  }, [])

  const loadAdminInfo = async () => {
    try {
      const { data: { session } } = await supabase.auth.getSession()
      
      const response = await fetch('/api/admin/me', {
        headers: {
          'Authorization': `Bearer ${session.access_token}`
        }
      })

      const adminData = await response.json()
      setAdmin(adminData)

    } catch (error) {
      console.error('Failed to load admin info:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleLogout = async () => {
    await supabase.auth.signOut()
    window.location.href = '/admin/login'
  }

  if (loading) {
    return <div>Loading...</div>
  }

  return (
    <div>
      <h1>Admin Dashboard</h1>
      <p>Welcome, {admin?.first_name} {admin?.last_name}!</p>
      <p>Email: {admin?.email}</p>
      <p>Role: {admin?.role}</p>
      <p>Status: {admin?.status}</p>
      
      <button onClick={handleLogout}>Logout</button>
    </div>
  )
}
```

## API Endpoints

The backend provides these endpoints for admin management:

- `GET /admin/auth/me` - Get current admin info
- `GET /admin/auth/requires-password-change` - Check if password change required
- `POST /admin/auth/mark-password-changed` - Mark password as changed
- `POST /admin/auth/create` - Create new admin (super admin only)
- `POST /admin/auth/forgot-password` - Request password reset
- `PUT /admin/auth/status` - Update admin status

## Environment Variables

Make sure to set these environment variables in your frontend:

```bash
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key
```

## Security Considerations

1. **Token Management**: Always use Supabase session tokens for API calls
2. **Password Requirements**: Enforce strong password requirements
3. **Session Handling**: Properly handle session expiration
4. **Error Handling**: Don't expose sensitive information in error messages
5. **HTTPS**: Always use HTTPS in production

## Flow Summary

1. **Initial Setup**: Super admin created with temporary password
2. **First Login**: Admin logs in with temporary password
3. **Password Change**: System detects first login and requires password change
4. **Status Update**: Backend marks admin as active after password change
5. **Normal Operation**: Admin uses new password for all subsequent logins 