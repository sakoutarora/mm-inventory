const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'https://dqrul4dok9.execute-api.ap-south-1.amazonaws.com/prod/api/v1').replace(/\/$/, '')

async function request(path, options = {}) {
  const headers = {
    ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
  }
  let body = undefined

  if (options.formData) {
    // Multipart — let browser set Content-Type with boundary
    body = options.formData
  } else {
    headers['Content-Type'] = 'application/json'
    body = options.body ? JSON.stringify(options.body) : undefined
  }

  const response = await fetch(`${API_BASE}${path}`, {
    headers,
    method: options.method || 'GET',
    body,
  })

  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.message || `Request failed (${response.status})`)
  }
  return payload
}

export const api = {
  login: (body) => request('/auth/login', { method: 'POST', body }),
  getInventoryItems: (branchCode, token) =>
    request(`/inventory/items?branchCode=${encodeURIComponent(branchCode)}`, { token }),
  submitInventoryUpdate: async (body, token) => {
    try {
      return await request('/inventory/update', { method: 'POST', body, token })
    } catch (error) {
      const details = error?.message ? ` (${error.message})` : ''
      throw new Error(`Update inventory failed${details}`)
    }
  },
  getDashboardSummary: (branchCode, token, days = 14) =>
    request(`/dashboard/summary?branchCode=${encodeURIComponent(branchCode)}&days=${days}`, { token }),
  getAdminMeta: (token) => request('/admin/meta', { token }),
  createCategory: (body, token) => request('/admin/categories', { method: 'POST', body, token }),
  createItem: (body, token) => request('/admin/items', { method: 'POST', body, token }),
  updateItem: (body, token) => request('/admin/items', { method: 'PATCH', body, token }),
  createUser: (body, token) => request('/admin/users', { method: 'POST', body, token }),

  // Suppliers
  getSuppliers: (token) => request('/suppliers', { token }),
  createSupplier: (body, token) => request('/suppliers', { method: 'POST', body, token }),

  // Bill Mappings
  getBillMappings: (params, token) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/bill-mappings${qs ? `?${qs}` : ''}`, { token })
  },
  createBillMapping: (body, token) => request('/bill-mappings', { method: 'POST', body, token }),
  suggestMapping: (description, token) =>
    request(`/bill-mappings/suggest?description=${encodeURIComponent(description)}`, { token }),

  // Item Pricing
  getItemPricing: (token) => request('/items/pricing', { token }),
  updateItemBasePrice: (itemId, body, token) =>
    request(`/items/${itemId}/base-price`, { method: 'PATCH', body, token }),

  // Bills
  parseHyperpure: (file, token) => {
    const formData = new FormData()
    formData.append('file', file)
    return request('/bills/parse-hyperpure', { method: 'POST', formData, token })
  },
  confirmBill: (body, token) => request('/bills/confirm', { method: 'POST', body, token }),
  updateBill: (billId, body, token) => request(`/bills/${billId}`, { method: 'PATCH', body, token }),
  getBills: (params, token) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/bills${qs ? `?${qs}` : ''}`, { token })
  },
  getBillDetail: (billId, token) => request(`/bills/${billId}`, { token }),
  deleteBill: (billId, token) => request(`/bills/${billId}`, { method: 'DELETE', token }),
  getDeletedBills: (params, token) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/bills/deleted${qs ? `?${qs}` : ''}`, { token })
  },
  getBillDownloadUrl: (billId, token) => request(`/bills/${billId}/download-url`, { token }),

  // Snapshots & Food Cost
  getSnapshots: (branchCode, token) =>
    request(`/inventory-updates/snapshots?branchCode=${encodeURIComponent(branchCode)}`, { token }),
  getFoodCost: (params, token) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/food-cost?${qs}`, { token })
  },

  // Update History
  getUpdateHistory: (branchCode, token, limit = 50, offset = 0) =>
    request(`/inventory-updates/history?branchCode=${encodeURIComponent(branchCode)}&limit=${limit}&offset=${offset}`, { token }),
  getUpdateDetail: (updateId, token) =>
    request(`/inventory-updates/${encodeURIComponent(updateId)}`, { token }),
}
