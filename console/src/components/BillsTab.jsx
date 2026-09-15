import { useEffect, useRef, useState, Fragment } from 'react'
import { api } from '../api'
import SearchableSelect from './SearchableSelect'

const TAX_SLABS = ['0', '5', '12', '18', '28', 'custom']

const STANDARD_UNITS = ['kg', 'gms', 'lt', 'ml', 'pcs', 'pkt', 'tray']

// Unit families for detecting mismatches
const UNIT_FAMILY = {
  kg: 'weight', gms: 'weight', g: 'weight',
  lt: 'volume', ml: 'volume', ltr: 'volume',
  pcs: 'count', pc: 'count', piece: 'count',
  pkt: 'packet', packet: 'packet',
  pack: 'pack', count: 'count',
  tray: 'count',
}

function getUnitFamily(unit) {
  return UNIT_FAMILY[(unit || '').toLowerCase()] || 'unknown'
}

export default function BillsTab({ session, onNavigate }) {
  const [view, setView] = useState('history') // 'history' | 'upload' | 'manual' | 'review'
  const [bills, setBills] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [toast, setToast] = useState(null)

  // Filters
  const [filterDateFrom, setFilterDateFrom] = useState('')
  const [filterDateTo, setFilterDateTo] = useState('')
  const [filterSource, setFilterSource] = useState('all')
  const [filterSupplier, setFilterSupplier] = useState('')

  // Upload state
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  // Manual entry state
  const [manualForm, setManualForm] = useState({
    supplierName: '', billDate: new Date().toISOString().slice(0, 10),
    billNumber: '', lineItems: [emptyLineItem()],
  })
  const [suppliers, setSuppliers] = useState([])

  // Review state (shared between HP and manual)
  const [reviewData, setReviewData] = useState(null)
  const [inventoryItems, setInventoryItems] = useState([])
  const [saving, setSaving] = useState(false)

  // Edit mode
  const [editingBillId, setEditingBillId] = useState(null)

  // Expanded bill detail
  const [expandedBillId, setExpandedBillId] = useState(null)
  const [billDetail, setBillDetail] = useState(null)

  // Deleted bills view + delete confirmation
  const [showDeleted, setShowDeleted] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState(null) // bill row pending confirmation
  const [deleting, setDeleting] = useState(false)

  // Suggestion fetch tracking
  const suggestionsRequested = useRef(false)
  const suggestionsCancelled = useRef(false)

  // Quick-add item modal
  const [quickAdd, setQuickAdd] = useState(null) // { lineItemUid, prefillName }
  const [quickAddForm, setQuickAddForm] = useState({ name: '', sku: '', defaultUnit: 'pcs', categoryCode: '', minThreshold: '0' })
  const [quickAddBusy, setQuickAddBusy] = useState(false)
  const [categories, setCategories] = useState([])

  useEffect(() => { loadBills() }, [])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3500)
    return () => clearTimeout(t)
  }, [toast])

  // Browser back button support for sub-views
  useEffect(() => {
    function onPopState(e) {
      if (e.state?.billsView) {
        setView(e.state.billsView)
      } else if (view !== 'history') {
        setView('history')
        setReviewData(null)
        setEditingBillId(null)
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [view])

  // Auto-suggest mappings for unmapped items when review data loads
  useEffect(() => {
    if (!reviewData || !inventoryItems.length || suggestionsRequested.current) return
    const unmapped = reviewData.lineItems
      .filter((li) => li.description && !li.inventoryItemId)
      .map((li) => ({ uid: li._uid, description: li.description }))
    if (!unmapped.length) return

    suggestionsRequested.current = true
    // Results are matched back by row uid, not by array position: the user can
    // delete or map a row while these requests are in flight, which shifts
    // every index after it. Cancellation is deliberately tied to unmount only
    // — this effect re-runs on every reviewData change, and cancelling there
    // would throw away the suggestions for all the other rows.
    suggestionsCancelled.current = false
    async function fetchSuggestions() {
      const results = await Promise.allSettled(
        unmapped.map((u) => api.suggestMapping(u.description, session.token))
      )
      if (suggestionsCancelled.current) return
      setReviewData((prev) => {
        if (!prev) return prev
        const items = [...prev.lineItems]
        results.forEach((result, i) => {
          if (result.status !== 'fulfilled') return
          const { suggestions, parsed } = result.value
          const top = suggestions?.[0]
          if (!top || top.confidence === 'low') return
          const lineIdx = items.findIndex((li) => li._uid === unmapped[i].uid)
          if (lineIdx === -1) return // row was deleted while the request was in flight
          if (items[lineIdx].inventoryItemId) return // already mapped

          const invItem = inventoryItems.find((it) => it.id === top.inventoryItemId)
          items[lineIdx] = {
            ...items[lineIdx],
            inventoryItemId: top.inventoryItemId,
            inventoryItemName: top.inventoryItemName || '',
            confidence: top.confidence === 'exact' ? 'high' : top.confidence,
            _mappedBaseUnit: invItem?.baseUnit || '',
            conversionToUnit: invItem?.baseUnit || '',
            _suggestion: { ...top, parsed },
          }
          // Apply conversion suggestion
          if (top.suggestedConversion?.factor) {
            items[lineIdx].conversionFactor = top.suggestedConversion.factor
            if (top.suggestedConversion.fromUnit) items[lineIdx].conversionFromUnit = top.suggestedConversion.fromUnit
            if (top.suggestedConversion.toUnit) items[lineIdx].conversionToUnit = top.suggestedConversion.toUnit
          }
        })
        return { ...prev, lineItems: items }
      })
    }
    fetchSuggestions()
  }, [reviewData, inventoryItems.length])

  useEffect(() => () => { suggestionsCancelled.current = true }, [])

  function emptyLineItem() {
    return {
      description: '', quantity: '', unitPrice: '', discount: '0',
      taxSlab: '0', taxAmount: '', uom: '', inventoryItemId: '', mappingSearch: '',
      conversionFactor: '1',
    }
  }

  async function loadBills(filters, deletedOverride) {
    setLoading(true)
    setError('')
    // showDeleted may not have flushed yet when the toggle triggers this call.
    const deleted = deletedOverride === undefined ? showDeleted : deletedOverride
    try {
      const params = { branchCode: session.branchCode }
      const f = filters || {}
      if (f.dateFrom || filterDateFrom) params.dateFrom = f.dateFrom || filterDateFrom
      if (f.dateTo || filterDateTo) params.dateTo = f.dateTo || filterDateTo
      if ((f.source || filterSource) !== 'all') params.source = f.source || filterSource
      if (f.supplierId || filterSupplier) params.supplierId = f.supplierId || filterSupplier
      const data = deleted
        ? await api.getDeletedBills(params, session.token)
        : await api.getBills(params, session.token)
      setBills(data.bills || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function loadSuppliers() {
    try {
      const data = await api.getSuppliers(session.token)
      setSuppliers(data.suppliers || [])
    } catch (_) {}
  }

  async function loadInventoryItems() {
    try {
      const data = await api.getItemPricing(session.token)
      setInventoryItems(data.items || [])
    } catch (_) {}
  }

  function applyFilters() {
    loadBills({ dateFrom: filterDateFrom, dateTo: filterDateTo, source: filterSource, supplierId: filterSupplier })
  }

  function clearFilters() {
    setFilterDateFrom('')
    setFilterDateTo('')
    setFilterSource('all')
    setFilterSupplier('')
    loadBills({ dateFrom: '', dateTo: '', source: 'all', supplierId: '' })
  }

  // ---- Quick-add item ----
  function openQuickAdd(lineItemUid, prefillName) {
    if (!categories.length) {
      api.getAdminMeta(session.token).then((data) => {
        setCategories(data.categories || [])
      }).catch(() => {})
    }
    const sku = prefillName
      .replace(/[^a-zA-Z0-9\s]/g, '')
      .trim()
      .toUpperCase()
      .replace(/\s+/g, '-')
      .slice(0, 20)
    setQuickAddForm({ name: prefillName, sku, defaultUnit: 'pcs', categoryCode: '', minThreshold: '0' })
    setQuickAdd({ lineItemUid, prefillName })
  }

  async function submitQuickAdd() {
    if (!quickAddForm.name || !quickAddForm.sku || !quickAddForm.defaultUnit) return
    setQuickAddBusy(true)
    try {
      await api.createItem({
        name: quickAddForm.name,
        sku: quickAddForm.sku,
        categoryCode: quickAddForm.categoryCode || undefined,
        defaultUnit: quickAddForm.defaultUnit,
        allowedUnits: quickAddForm.defaultUnit,
        minThreshold: Number(quickAddForm.minThreshold) || 0,
        isRequired: false,
      }, session.token)
      await loadInventoryItems()
      // Find the newly created item and map it
      const newItem = inventoryItems.find((i) => i.sku === quickAddForm.sku)
        || (await api.getItemPricing(session.token)).items?.find((i) => i.sku === quickAddForm.sku)
      if (newItem && quickAdd) {
        updateReviewItemByUid(quickAdd.lineItemUid, 'inventoryItemId', newItem.id)
        // Refresh the items list so the new item shows
        const refreshed = await api.getItemPricing(session.token)
        setInventoryItems(refreshed.items || [])
      }
      setToast({ type: 'success', message: `Item "${quickAddForm.name}" created` })
      setQuickAdd(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setQuickAddBusy(false)
    }
  }

  // Stable per-row identity. Review rows are reorderable (a row can be
  // deleted), so an array index is not a safe React key — SearchableSelect
  // keeps internal search state that would leak to the row taking that slot.
  const reviewRowUid = useRef(0)
  function nextRowUid() {
    reviewRowUid.current += 1
    return `row-${reviewRowUid.current}`
  }

  // ---- Helper: find saved conversion for item+unit ----
  function getSavedConversion(itemId, billUnit) {
    if (!itemId || !billUnit) return null
    const item = inventoryItems.find((i) => i.id === itemId)
    if (!item || !item.unitConversions) return null
    const canon = billUnit.toLowerCase()
    return item.unitConversions[canon] || null
  }

  // ---- Hyperpure Upload ----
  async function handleFileUpload(file) {
    if (!file) return
    setUploading(true)
    setError('')
    try {
      const result = await api.parseHyperpure(file, session.token)
      await loadInventoryItems()
      const reviewItems = (result.lineItems || []).map((li) => {
        const mappedItemId = li.suggestedMapping?.inventoryItemId || ''
        const mappedItem = mappedItemId ? inventoryItems.find((i) => i.id === mappedItemId) : null
        return {
          ...li,
          _uid: nextRowUid(),
          inventoryItemId: mappedItemId,
          inventoryItemName: li.suggestedMapping?.inventoryItemName || '',
          confidence: li.suggestedMapping?.confidence || 'none',
          conversionFactor: li.suggestedMapping?.existingConversion?.factor || 1,
          conversionFromUnit: li.suggestedMapping?.existingConversion?.fromUnit || li.uom || '',
          conversionToUnit: li.suggestedMapping?.existingConversion?.toUnit || (mappedItem?.baseUnit || ''),
          saveGlobalMapping: true,
          _mappedBaseUnit: mappedItem?.baseUnit || '',
        }
      })
      setReviewData({
        source: 'hyperpure',
        supplierName: 'Zomato Hyperpure',
        orderNo: result.billMeta?.orderNo || '',
        billDate: result.billMeta?.invoiceDate || '',
        orderDate: result.billMeta?.orderDate || '',
        paymentStatus: result.billMeta?.paymentStatus || 'unpaid',
        lineItems: reviewItems,
        ignoredLines: result.ignoredLines || [],
        duplicateLines: result.duplicateLines || [],
        totals: result.totals || {},
      })
      setEditingBillId(null)
      suggestionsRequested.current = false
      history.pushState({ billsView: 'review' }, '')
      setView('review')
    } catch (err) {
      setError(err.message)
    } finally {
      setUploading(false)
    }
  }

  function handleDrop(e) {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer?.files?.[0]
    if (file && file.type === 'application/pdf') handleFileUpload(file)
    else setError('Please upload a PDF file.')
  }

  // ---- Manual Entry ----
  function openManualEntry() {
    loadSuppliers()
    loadInventoryItems()
    setManualForm({
      supplierName: '', billDate: new Date().toISOString().slice(0, 10),
      billNumber: '', lineItems: [emptyLineItem()],
    })
    history.pushState({ billsView: 'manual' }, '')
    setView('manual')
  }

  function updateManualLineItem(idx, field, value) {
    setManualForm((prev) => {
      const items = [...prev.lineItems]
      items[idx] = { ...items[idx], [field]: value }

      // Auto-fill conversion when mapping item or changing unit
      if (field === 'inventoryItemId' || field === 'uom') {
        const itemId = field === 'inventoryItemId' ? value : items[idx].inventoryItemId
        const unit = field === 'uom' ? value : items[idx].uom
        if (itemId && unit) {
          const mappedItem = inventoryItems.find((i) => i.id === itemId)
          if (mappedItem) {
            const mismatch = getUnitFamily(unit) !== getUnitFamily(mappedItem.baseUnit)
            if (mismatch) {
              const saved = getSavedConversion(itemId, unit)
              if (saved) items[idx].conversionFactor = String(saved)
            } else {
              items[idx].conversionFactor = '1'
            }
          }
        }
      }

      const qty = Number(items[idx].quantity) || 0
      const price = Number(items[idx].unitPrice) || 0
      const disc = Number(items[idx].discount) || 0
      const taxable = qty * price - disc
      if (items[idx].taxSlab !== 'custom') {
        const rate = Number(items[idx].taxSlab) || 0
        items[idx].taxAmount = String(Math.round(taxable * rate / 100 * 100) / 100)
      }

      return { ...prev, lineItems: items }
    })
  }

  function addManualLineItem() {
    setManualForm((prev) => ({ ...prev, lineItems: [...prev.lineItems, emptyLineItem()] }))
  }

  function removeManualLineItem(idx) {
    setManualForm((prev) => ({
      ...prev,
      lineItems: prev.lineItems.filter((_, i) => i !== idx),
    }))
  }

  function clearManualLineItems() {
    setManualForm((prev) => ({ ...prev, lineItems: [emptyLineItem()] }))
  }

  function submitManualForReview() {
    const lineItems = manualForm.lineItems.map((li, i) => {
      const qty = Number(li.quantity) || 0
      const price = Number(li.unitPrice) || 0
      const disc = Number(li.discount) || 0
      const taxAmt = Number(li.taxAmount) || 0
      const taxable = qty * price - disc
      const mappedItem = li.inventoryItemId ? inventoryItems.find((it) => it.id === li.inventoryItemId) : null
      const mismatch = mappedItem && li.uom && getUnitFamily(li.uom) !== getUnitFamily(mappedItem.baseUnit)
      const factor = mismatch ? (Number(li.conversionFactor) || 1) : 1
      return {
        slNo: i + 1,
        description: li.description,
        hsnCode: null,
        quantity: qty,
        unitPrice: price,
        uom: li.uom,
        preTaxTotal: qty * price,
        discount: disc,
        taxableAmount: taxable,
        taxRate: { cgst: 0, sgst: 0, igst: 0, cess: 0 },
        taxAmount: taxAmt,
        total: taxable + taxAmt,
        inventoryItemId: li.inventoryItemId,
        inventoryItemName: mappedItem?.name || '',
        confidence: li.inventoryItemId ? 'high' : 'none',
        conversionFactor: factor,
        conversionFromUnit: li.uom,
        conversionToUnit: mappedItem?.baseUnit || '',
        saveGlobalMapping: true,
        _mappedBaseUnit: mappedItem?.baseUnit || '',
        _uid: nextRowUid(),
      }
    })

    setReviewData({
      source: 'generic',
      supplierName: manualForm.supplierName,
      orderNo: null,
      billNumber: manualForm.billNumber,
      billDate: manualForm.billDate,
      orderDate: null,
      paymentStatus: 'unpaid',
      lineItems,
      ignoredLines: [],
      totals: {
        subtotal: lineItems.reduce((s, i) => s + i.preTaxTotal, 0),
        totalDiscount: lineItems.reduce((s, i) => s + i.discount, 0),
        taxableAmount: lineItems.reduce((s, i) => s + i.taxableAmount, 0),
        taxAmount: lineItems.reduce((s, i) => s + i.taxAmount, 0),
        grandTotal: lineItems.reduce((s, i) => s + i.total, 0),
      },
    })
    setEditingBillId(null)
    suggestionsRequested.current = false
    history.pushState({ billsView: 'review' }, '')
    setView('review')
  }

  // ---- Edit Bill ----
  async function startEditBill(billId) {
    setError('')
    try {
      const data = await api.getBillDetail(billId, session.token)
      await loadInventoryItems()
      const bill = data.bill
      const reviewItems = (bill.lineItems || []).map((li) => {
        const mappedItem = li.inventoryItemId ? inventoryItems.find((i) => i.id === li.inventoryItemId) : null
        return {
          _uid: nextRowUid(),
          slNo: li.slNo,
          description: li.description,
          hsnCode: li.hsnCode,
          quantity: li.quantity,
          unitPrice: li.unitPrice,
          uom: li.uom,
          preTaxTotal: li.preTaxTotal,
          discount: li.discount,
          taxableAmount: li.taxableAmount,
          taxRate: li.taxRate || { cgst: 0, sgst: 0, igst: 0, cess: 0 },
          taxAmount: li.taxAmount,
          total: li.total,
          inventoryItemId: li.inventoryItemId || '',
          inventoryItemName: li.inventoryItemName || '',
          confidence: li.inventoryItemId ? 'high' : 'none',
          conversionFactor: li.conversionFactor || 1,
          conversionFromUnit: li.conversionFromUnit || li.uom || '',
          conversionToUnit: li.conversionToUnit || (mappedItem?.baseUnit || ''),
          saveGlobalMapping: true,
          _mappedBaseUnit: mappedItem?.baseUnit || li.conversionToUnit || '',
        }
      })
      setReviewData({
        source: bill.source,
        supplierName: bill.supplierName,
        orderNo: bill.orderNo,
        billNumber: bill.billNumber,
        billDate: bill.billDate ? new Date(bill.billDate).toISOString().slice(0, 10) : '',
        orderDate: bill.orderDate,
        paymentStatus: bill.paymentStatus || 'unpaid',
        lineItems: reviewItems,
        ignoredLines: [],
        totals: {
          subtotal: bill.subtotal,
          totalDiscount: bill.totalDiscount,
          taxableAmount: bill.totalTaxableAmount,
          taxAmount: bill.totalTaxAmount,
          grandTotal: bill.grandTotal,
        },
      })
      setEditingBillId(billId)
      suggestionsRequested.current = false
      history.pushState({ billsView: 'review' }, '')
      setView('review')
    } catch (err) {
      setError(err.message)
    }
  }

  // ---- Confirm / Update Bill ----
  // Totals come from the PDF, so they must be recomputed whenever a parsed
  // row is removed — otherwise the saved grand total includes deleted items.
  function recalcTotals(items, prevTotals) {
    const round2 = (n) => Math.round(n * 100) / 100
    const sum = (field) => round2(items.reduce((acc, li) => acc + (Number(li[field]) || 0), 0))
    const itemsTotal = sum('total')
    const otherCharges = round2(Number(prevTotals?.otherCharges) || 0)
    return {
      ...prevTotals,
      subtotal: sum('preTaxTotal'),
      totalDiscount: sum('discount'),
      taxableAmount: sum('taxableAmount'),
      taxAmount: sum('taxAmount'),
      itemsTotal,
      otherCharges,
      grandTotal: round2(itemsTotal + otherCharges),
    }
  }

  function removeReviewItem(idx) {
    setReviewData((prev) => {
      const items = prev.lineItems
        .filter((_, i) => i !== idx)
        .map((li, i) => ({ ...li, slNo: i + 1 }))
      return { ...prev, lineItems: items, totals: recalcTotals(items, prev.totals) }
    })
  }

  // For callers that resolve after an await: the row may have moved or been
  // deleted since, so look it up by uid rather than trusting a stale index.
  function updateReviewItemByUid(uid, field, value) {
    setReviewData((prev) => {
      if (!prev) return prev
      const idx = prev.lineItems.findIndex((li) => li._uid === uid)
      if (idx === -1) return prev
      return applyReviewItemChange(prev, idx, field, value)
    })
  }

  function updateReviewItem(idx, field, value) {
    setReviewData((prev) => applyReviewItemChange(prev, idx, field, value))
  }

  function applyReviewItemChange(prev, idx, field, value) {
    const items = [...prev.lineItems]
    items[idx] = { ...items[idx], [field]: value }
    if (field === 'inventoryItemId') {
      const found = inventoryItems.find((i) => i.id === value)
      items[idx].inventoryItemName = found?.name || ''
      items[idx].confidence = value ? 'high' : 'none'
      items[idx]._mappedBaseUnit = found?.baseUnit || ''
      items[idx].conversionToUnit = found?.baseUnit || ''
      // Auto-fill saved conversion
      if (value && items[idx].uom) {
        const mismatch = found && getUnitFamily(items[idx].uom) !== getUnitFamily(found.baseUnit)
        if (mismatch) {
          const saved = getSavedConversion(value, items[idx].uom)
          if (saved) items[idx].conversionFactor = saved
        } else {
          items[idx].conversionFactor = 1
        }
      }
    }
    return { ...prev, lineItems: items }
  }

  async function confirmBill() {
    if (!reviewData) return
    if (!reviewData.lineItems.length) {
      setError('This bill has no line items. Add at least one item before saving.')
      return
    }
    const unmapped = reviewData.lineItems.filter((li) => !li.inventoryItemId)
    if (unmapped.length) {
      setError(`${unmapped.length} item(s) are not mapped to inventory. Map all items before saving.`)
      return
    }

    setSaving(true)
    setError('')
    try {
      const lineItemsPayload = reviewData.lineItems.map((li) => ({
        slNo: li.slNo,
        description: li.description,
        hsnCode: li.hsnCode,
        quantity: li.quantity,
        unitPrice: li.unitPrice,
        uom: li.uom,
        preTaxTotal: li.preTaxTotal,
        discount: li.discount,
        taxableAmount: li.taxableAmount,
        taxRate: li.taxRate,
        taxAmount: li.taxAmount,
        total: li.total,
        inventoryItemId: li.inventoryItemId,
        conversionFactor: li.conversionFactor || 1,
        conversionFromUnit: li.conversionFromUnit || '',
        conversionToUnit: li.conversionToUnit || '',
        saveGlobalMapping: li.saveGlobalMapping !== false,
      }))

      if (editingBillId) {
        // Update existing bill
        await api.updateBill(editingBillId, {
          supplierName: reviewData.supplierName,
          billDate: reviewData.billDate,
          billNumber: reviewData.billNumber,
          paymentStatus: reviewData.paymentStatus,
          grandTotal: reviewData.totals?.grandTotal || 0,
          otherCharges: reviewData.totals?.otherCharges || 0,
          lineItems: lineItemsPayload,
        }, session.token)
        setToast({ type: 'success', message: `Bill updated - ${lineItemsPayload.length} items` })
      } else {
        // Create new bill
        const body = {
          branchCode: session.branchCode,
          source: reviewData.source,
          supplierName: reviewData.supplierName,
          orderNo: reviewData.orderNo,
          billNumber: reviewData.billNumber,
          billDate: reviewData.billDate,
          orderDate: reviewData.orderDate,
          paymentStatus: reviewData.paymentStatus,
          grandTotal: reviewData.totals?.grandTotal || 0,
          otherCharges: reviewData.totals?.otherCharges || 0,
          lineItems: lineItemsPayload,
        }
        await api.confirmBill(body, session.token)
        setToast({ type: 'success', message: `Bill saved - ${lineItemsPayload.length} items mapped to inventory` })
      }

      setView('history')
      setReviewData(null)
      setEditingBillId(null)
      await loadBills()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  // ---- Bill Detail ----
  async function toggleBillDetail(billId) {
    if (expandedBillId === billId) {
      setExpandedBillId(null)
      setBillDetail(null)
      return
    }
    try {
      const data = await api.getBillDetail(billId, session.token)
      setBillDetail(data.bill)
      setExpandedBillId(billId)
    } catch (err) {
      setError(err.message)
    }
  }

  async function downloadBill(billId) {
    try {
      const data = await api.getBillDownloadUrl(billId, session.token)
      if (data.url) window.open(data.url, '_blank')
    } catch (err) {
      setError(err.message)
    }
  }

  // ---- Delete ----
  async function confirmDeleteBill() {
    if (!deleteTarget) return
    setDeleting(true)
    setError('')
    try {
      await api.deleteBill(deleteTarget.id, session.token)
      setDeleteTarget(null)
      if (expandedBillId === deleteTarget.id) {
        setExpandedBillId(null)
        setBillDetail(null)
      }
      setToast({ type: 'success', message: 'Bill deleted' })
      await loadBills()
    } catch (err) {
      setError(err.message)
    } finally {
      setDeleting(false)
    }
  }

  function toggleDeletedView() {
    const next = !showDeleted
    setShowDeleted(next)
    setExpandedBillId(null)
    setBillDetail(null)
    setDeleteTarget(null)
    loadBills(null, next)
  }

  // ---- Helpers ----
  function ConfidenceBadge({ confidence }) {
    if (confidence === 'high') return <span style={{ color: '#16a34a' }}>&#10003;</span>
    if (confidence === 'medium') return <span style={{ color: '#d97706' }}>&#9888;</span>
    return <span style={{ color: '#dc2626' }}>&#10007;</span>
  }

  function hasUnitMismatch(billUom, mappedBaseUnit) {
    if (!billUom || !mappedBaseUnit) return false
    return getUnitFamily(billUom) !== getUnitFamily(mappedBaseUnit)
  }

  const inventoryItemOptions = inventoryItems.map((item) => ({
    value: item.id,
    label: `${item.name} (${item.sku})`,
  }))

  const supplierOptions = suppliers.map((s) => ({
    value: s.name,
    label: s.name,
  }))

  // ========== RENDER ==========

  // Upload view
  if (view === 'upload') {
    return (
      <section className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <div>
            <h3 style={{ margin: 0 }}>Upload Hyperpure Challan</h3>
            <p style={{ color: '#64748b', fontSize: 13, margin: '4px 0 0' }}>Upload a PDF challan from Zomato Hyperpure to auto-extract line items</p>
          </div>
          <button className="btn-secondary" onClick={() => history.back()}>Back</button>
        </div>
        {error && <div className="banner error">{error}</div>}

        <div
          onDrop={handleDrop}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          className={`drop-zone ${dragOver ? 'drop-zone-active' : ''}`}
          onClick={() => {
            const input = document.createElement('input')
            input.type = 'file'
            input.accept = '.pdf'
            input.onchange = (e) => handleFileUpload(e.target.files[0])
            input.click()
          }}
        >
          {uploading ? (
            <div style={{ textAlign: 'center' }}>
              <div className="spinner" style={{ margin: '0 auto 12px' }} />
              <p style={{ fontSize: 18, fontWeight: 600 }}>Parsing challan...</p>
              <p style={{ color: '#64748b' }}>Extracting line items and matching inventory</p>
            </div>
          ) : (
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 48, marginBottom: 12, opacity: 0.8 }}>
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                  <line x1="12" y1="18" x2="12" y2="12" />
                  <line x1="9" y1="15" x2="12" y2="12" />
                  <line x1="15" y1="15" x2="12" y2="12" />
                </svg>
              </div>
              <p style={{ fontSize: 18, fontWeight: 600, marginBottom: 4 }}>Drop Hyperpure Challan PDF here</p>
              <p style={{ color: '#64748b' }}>or click to browse files</p>
              <p style={{ color: '#94a3b8', fontSize: 12, marginTop: 12 }}>Supports .pdf files only</p>
            </div>
          )}
        </div>

        <div style={{ marginTop: 20, padding: '16px 20px', background: '#f8fafc', borderRadius: 8, border: '1px solid #e2e8f0' }}>
          <p style={{ fontSize: 13, color: '#64748b', margin: 0 }}>
            <strong>How it works:</strong> Upload your Hyperpure challan PDF and we will automatically extract all line items,
            match them to your inventory, and let you review before saving.
          </p>
        </div>
      </section>
    )
  }

  // Manual entry view
  if (view === 'manual') {
    const manualGrandTotal = manualForm.lineItems.reduce((sum, li) => {
      const qty = Number(li.quantity) || 0
      const price = Number(li.unitPrice) || 0
      const disc = Number(li.discount) || 0
      const taxAmt = Number(li.taxAmount) || 0
      return sum + (qty * price - disc + taxAmt)
    }, 0)

    return (
      <section className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <div>
            <h3 style={{ margin: 0 }}>Add Generic Bill</h3>
            <p style={{ color: '#64748b', fontSize: 13, margin: '4px 0 0' }}>Enter bill details and line items manually</p>
          </div>
          <button className="btn-secondary" onClick={() => history.back()}>Back</button>
        </div>
        {error && <div className="banner error">{error}</div>}

        {/* Bill details card */}
        <div style={{ background: '#f8fafc', borderRadius: 10, border: '1px solid #e2e8f0', padding: 20, marginBottom: 24 }}>
          <h4 style={{ margin: '0 0 16px', fontSize: 14, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Bill Details</h4>
          <div className="form-grid-3">
            <div className="field">
              <label>Supplier <span style={{ color: '#dc2626' }}>*</span></label>
              <SearchableSelect
                options={supplierOptions}
                value={manualForm.supplierName}
                onChange={(val) => setManualForm((p) => ({ ...p, supplierName: val }))}
                placeholder="Search or type new supplier..."
                allowFreeText={true}
              />
            </div>
            <div className="field">
              <label>Bill Date <span style={{ color: '#dc2626' }}>*</span></label>
              <input type="date" value={manualForm.billDate}
                onChange={(e) => setManualForm((p) => ({ ...p, billDate: e.target.value }))} />
            </div>
            <div className="field">
              <label>Bill Number</label>
              <input value={manualForm.billNumber} placeholder="e.g. INV-001"
                onChange={(e) => setManualForm((p) => ({ ...p, billNumber: e.target.value }))} />
            </div>
          </div>
        </div>

        {/* Line items as cards */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h4 style={{ margin: 0 }}>Line Items ({manualForm.lineItems.length})</h4>
          <button className="btn-secondary" onClick={addManualLineItem}>+ Add Item</button>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {manualForm.lineItems.map((li, idx) => {
            const qty = Number(li.quantity) || 0
            const price = Number(li.unitPrice) || 0
            const disc = Number(li.discount) || 0
            const taxAmt = Number(li.taxAmount) || 0
            const taxable = qty * price - disc
            const total = taxable + taxAmt
            const mappedItem = li.inventoryItemId ? inventoryItems.find((it) => it.id === li.inventoryItemId) : null
            const mismatch = mappedItem && li.uom && hasUnitMismatch(li.uom, mappedItem.baseUnit)

            return (
              <div key={idx} style={{ background: '#fff', borderRadius: 10, border: '1px solid #e2e8f0', padding: 16, position: 'relative' }}>
                {/* Item number & remove */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                  <span style={{ fontSize: 12, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase' }}>Item {idx + 1}</span>
                  <button className="btn-small" style={{ color: '#dc2626', opacity: manualForm.lineItems.length <= 1 ? 0.3 : 1 }}
                    onClick={() => removeManualLineItem(idx)}
                    disabled={manualForm.lineItems.length <= 1}>&#10005; Remove</button>
                </div>

                {/* Row 1: Description + Mapped Item */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                  <div className="field">
                    <label>Description <span style={{ color: '#dc2626' }}>*</span></label>
                    <input value={li.description} onChange={(e) => updateManualLineItem(idx, 'description', e.target.value)}
                      placeholder="e.g. Basmati Rice 5kg" />
                  </div>
                  <div className="field">
                    <label>Map to Inventory Item</label>
                    <SearchableSelect
                      options={inventoryItemOptions}
                      value={li.inventoryItemId}
                      onChange={(val) => updateManualLineItem(idx, 'inventoryItemId', val)}
                      placeholder="Search item..."
                    />
                  </div>
                </div>

                {/* Row 2: Qty, Unit Price, Unit, Discount */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
                  <div className="field">
                    <label>Quantity</label>
                    <input type="number" value={li.quantity} onChange={(e) => updateManualLineItem(idx, 'quantity', e.target.value)}
                      placeholder="0" />
                  </div>
                  <div className="field">
                    <label>Unit Price ({'\u20B9'})</label>
                    <input type="number" value={li.unitPrice} onChange={(e) => updateManualLineItem(idx, 'unitPrice', e.target.value)}
                      placeholder="0.00" />
                  </div>
                  <div className="field">
                    <label>Unit</label>
                    <select value={li.uom} onChange={(e) => updateManualLineItem(idx, 'uom', e.target.value)}>
                      <option value="">Select unit</option>
                      {STANDARD_UNITS.map((u) => <option key={u} value={u}>{u}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label>Discount ({'\u20B9'})</label>
                    <input type="number" value={li.discount} onChange={(e) => updateManualLineItem(idx, 'discount', e.target.value)}
                      placeholder="0" />
                  </div>
                </div>

                {/* Row 3: Tax + Total */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
                  <div className="field">
                    <label>Tax Slab</label>
                    <select value={li.taxSlab} onChange={(e) => updateManualLineItem(idx, 'taxSlab', e.target.value)}>
                      {TAX_SLABS.map((s) => <option key={s} value={s}>{s === 'custom' ? 'Custom' : `${s}%`}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label>Tax Amount ({'\u20B9'})</label>
                    {li.taxSlab === 'custom'
                      ? <input type="number" value={li.taxAmount} onChange={(e) => updateManualLineItem(idx, 'taxAmount', e.target.value)} placeholder="0" />
                      : <input type="text" value={li.taxAmount || '0'} disabled style={{ background: '#f1f5f9', color: '#64748b' }} />
                    }
                  </div>
                  <div className="field">
                    <label>Line Total</label>
                    <div style={{ padding: '8px 12px', background: '#f0fdf4', borderRadius: 6, fontWeight: 700, color: '#16a34a', border: '1px solid #bbf7d0' }}>
                      {'\u20B9'}{total.toFixed(2)}
                    </div>
                  </div>
                </div>

                {/* Unit mismatch warning */}
                {mismatch && (
                  <div style={{ marginTop: 12, padding: '10px 14px', background: '#fffbeb', borderRadius: 8, border: '1px solid #fde68a', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    <span style={{ color: '#d97706' }}>&#9888;</span>
                    <span style={{ fontSize: 13 }}>Unit mismatch: bill &ldquo;{li.uom}&rdquo; vs inventory &ldquo;{mappedItem.baseUnit}&rdquo;</span>
                    <span style={{ color: '#94a3b8' }}>|</span>
                    <span style={{ fontSize: 13 }}>1 {li.uom} =</span>
                    <input
                      type="number" step="0.01" min="0"
                      value={li.conversionFactor}
                      onChange={(e) => updateManualLineItem(idx, 'conversionFactor', e.target.value)}
                      style={{ width: 70 }}
                      placeholder="e.g. 3"
                    />
                    <span style={{ fontSize: 13 }}>{mappedItem.baseUnit}</span>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {/* Footer with totals and actions */}
        <div style={{ marginTop: 20, padding: '16px 20px', background: '#f8fafc', borderRadius: 10, border: '1px solid #e2e8f0', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <span style={{ fontSize: 13, color: '#64748b' }}>Grand Total: </span>
            <span style={{ fontSize: 20, fontWeight: 700 }}>{'\u20B9'}{manualGrandTotal.toFixed(2)}</span>
            <span style={{ fontSize: 13, color: '#94a3b8', marginLeft: 8 }}>({manualForm.lineItems.length} item{manualForm.lineItems.length !== 1 ? 's' : ''})</span>
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn-secondary" style={{ color: '#dc2626' }} onClick={clearManualLineItems}>Clear All</button>
            <button className="btn-primary" onClick={submitManualForReview}
              disabled={!manualForm.supplierName || manualForm.lineItems.every((li) => !li.description)}>
              Review & Confirm
            </button>
          </div>
        </div>
      </section>
    )
  }

  // Review & Map view
  if (view === 'review' && reviewData) {
    const unmappedCount = reviewData.lineItems.filter((li) => !li.inventoryItemId).length
    const mappedCount = reviewData.lineItems.length - unmappedCount

    return (
      <section className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <h3 style={{ margin: 0 }}>{editingBillId ? 'Edit Bill' : 'Review & Map Bill'}</h3>
          <button className="btn-secondary" onClick={() => history.back()}>Cancel</button>
        </div>
        {error && <div className="banner error">{error}</div>}

        {/* Bill header */}
        <div className="review-header-bar">
          <div className="review-header-item">
            <span className="review-header-label">Supplier</span>
            <span className="review-header-value">{reviewData.supplierName}</span>
          </div>
          {reviewData.orderNo && (
            <div className="review-header-item">
              <span className="review-header-label">Order #</span>
              <span className="review-header-value">{reviewData.orderNo}</span>
            </div>
          )}
          {reviewData.billNumber && (
            <div className="review-header-item">
              <span className="review-header-label">Bill #</span>
              <span className="review-header-value">{reviewData.billNumber}</span>
            </div>
          )}
          <div className="review-header-item">
            <span className="review-header-label">Date</span>
            <span className="review-header-value">{reviewData.billDate || '--'}</span>
          </div>
          <div className="review-header-item">
            <span className="review-header-label">Status</span>
            <span className={`review-status-badge ${reviewData.paymentStatus === 'paid' ? 'paid' : 'unpaid'}`}>
              {reviewData.paymentStatus}
            </span>
          </div>
        </div>

        {/* Mapping progress */}
        <div className="mapping-progress-bar">
          <div className="mapping-progress-track">
            <div
              className="mapping-progress-fill"
              style={{ width: `${reviewData.lineItems.length ? (mappedCount / reviewData.lineItems.length) * 100 : 0}%` }}
            />
          </div>
          <span className="mapping-progress-text">
            {mappedCount}/{reviewData.lineItems.length} mapped
          </span>
        </div>

        {/* Line items as cards */}
        <div className="review-items-list">
          {reviewData.lineItems.map((li, idx) => {
            const isMapped = !!li.inventoryItemId
            const mappedItem = isMapped ? inventoryItems.find((i) => i.id === li.inventoryItemId) : null
            const needsConversion = isMapped && li.uom && li._mappedBaseUnit &&
              li.uom.toLowerCase() !== li._mappedBaseUnit.toLowerCase()
            const hasFamilyMismatch = isMapped && hasUnitMismatch(li.uom, li._mappedBaseUnit)

            return (
              <div key={li._uid || idx} className={`review-item-card ${isMapped ? 'mapped' : 'unmapped'}`}>
                {/* Main row */}
                <div className="review-item-top">
                  <div className="review-item-num">{li.slNo}</div>
                  <div className="review-item-desc">
                    <div className="review-item-name">{li.description?.replace(/\n/g, ' ')}</div>
                    <div className="review-item-details">
                      <span>{li.quantity} {li.uom || 'units'}</span>
                      <span className="review-detail-sep">&times;</span>
                      <span>{'\u20B9'}{li.unitPrice}</span>
                      {li.discount > 0 && (
                        <>
                          <span className="review-detail-sep">-</span>
                          <span className="review-item-discount">{'\u20B9'}{li.discount} disc</span>
                        </>
                      )}
                      {li.taxAmount > 0 && (
                        <>
                          <span className="review-detail-sep">+</span>
                          <span>{'\u20B9'}{li.taxAmount?.toFixed?.(2) ?? li.taxAmount} tax</span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="review-item-total">
                    {'\u20B9'}{li.total?.toFixed?.(2) ?? li.total}
                  </div>
                  <button
                    type="button"
                    className="review-item-remove"
                    onClick={() => removeReviewItem(idx)}
                    title="Remove this row from the bill"
                    aria-label={`Remove ${li.description?.replace(/\n/g, ' ') || 'line item'}`}
                  >
                    &#10005;
                  </button>
                </div>

                {/* Mapping section */}
                <div className="review-item-mapping">
                  <div className="review-mapping-row">
                    <span className={`review-mapping-indicator ${isMapped ? 'mapped' : 'unmapped'}`}>
                      {isMapped ? '\u2713' : '\u2192'}
                    </span>
                    <div className="review-mapping-select-wrap">
                      <SearchableSelect
                        options={inventoryItemOptions}
                        value={li.inventoryItemId || ''}
                        onChange={(val) => updateReviewItem(idx, 'inventoryItemId', val)}
                        placeholder="Search inventory item to map..."
                        onCreateNew={(name) => openQuickAdd(li._uid, name || li.description?.replace(/\n/g, ' ') || '')}
                      />
                    </div>
                  </div>

                  {/* Conversion factor */}
                  {needsConversion && (
                    <div className={`review-conversion ${hasFamilyMismatch ? 'warning' : 'info'}`}>
                      <div className="review-conversion-label">
                        {hasFamilyMismatch && <span className="review-conversion-icon">&#9888;</span>}
                        <span>
                          {hasFamilyMismatch ? 'Unit mismatch' : 'Unit conversion'}:
                          bill uses <strong>{li.uom}</strong>, inventory uses <strong>{li._mappedBaseUnit}</strong>
                        </span>
                      </div>
                      <div className="review-conversion-input">
                        <span>1 {li.uom} =</span>
                        <input
                          type="number" step="0.01" min="0"
                          value={li.conversionFactor || ''}
                          onChange={(e) => updateReviewItem(idx, 'conversionFactor', Number(e.target.value))}
                          placeholder="?"
                          className="review-conversion-field"
                        />
                        <span>{li._mappedBaseUnit}</span>
                        {li.suggestedMapping?.existingConversion?.factor && (
                          <button
                            className="btn-small btn-secondary"
                            onClick={() => updateReviewItem(idx, 'conversionFactor', li.suggestedMapping.existingConversion.factor)}
                          >
                            Use saved ({li.suggestedMapping.existingConversion.factor})
                          </button>
                        )}
                      </div>
                      {li.conversionFactor > 0 && (
                        <div className="review-conversion-preview">
                          {li.quantity} {li.uom} = {(li.quantity * li.conversionFactor).toFixed(2)} {li._mappedBaseUnit}
                          {' '}at {'\u20B9'}{(li.unitPrice / li.conversionFactor).toFixed(2)}/{li._mappedBaseUnit}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {reviewData.duplicateLines?.length > 0 && (
          <div className="excluded-lines duplicate-lines">
            <strong>Duplicates removed ({reviewData.duplicateLines.length}):</strong>
            {reviewData.duplicateLines.map((dl, i) => (
              <span key={i} className="excluded-item">
                {dl.description?.replace(/\n/g, ' ')} ({'\u20B9'}{dl.total?.toFixed?.(2) ?? dl.total})
              </span>
            ))}
          </div>
        )}

        {reviewData.ignoredLines?.length > 0 && (
          <div className="excluded-lines">
            <strong>Excluded:</strong>
            {reviewData.ignoredLines.filter((il) => il.description.toLowerCase() !== 'total').map((il, i) => (
              <span key={i} className="excluded-item">
                {il.description} ({'\u20B9'}{il.total?.toFixed?.(2) ?? il.total})
              </span>
            ))}
          </div>
        )}

        <div className="review-footer">
          <div className="review-footer-total">
            <span className="review-footer-amount">
              Grand Total: {'\u20B9'}{reviewData.totals?.grandTotal?.toFixed?.(2) ?? reviewData.totals?.grandTotal}
            </span>
            <span className="review-footer-count">
              {reviewData.lineItems.length} items
              {unmappedCount > 0 && <span className="review-footer-unmapped"> &middot; {unmappedCount} unmapped</span>}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <button className="btn-secondary" onClick={() => history.back()}>Cancel</button>
            <button className="btn-primary" disabled={saving || unmappedCount > 0 || reviewData.lineItems.length === 0} onClick={confirmBill}>
              {saving ? 'Saving...' : editingBillId ? 'Update Bill' : 'Confirm & Save Bill'}
            </button>
          </div>
        </div>

        {/* Quick-add item modal */}
        {quickAdd && (
          <div className="quick-add-overlay" onClick={() => setQuickAdd(null)}>
            <div className="quick-add-modal" onClick={(e) => e.stopPropagation()}>
              <div className="quick-add-header">
                <h4 style={{ margin: 0 }}>Add New Inventory Item</h4>
                <button className="searchable-select-clear" onClick={() => setQuickAdd(null)} style={{ fontSize: 16 }}>&#10005;</button>
              </div>
              <div className="quick-add-body">
                <div className="field">
                  <label>Item Name</label>
                  <input value={quickAddForm.name} onChange={(e) => setQuickAddForm((p) => ({ ...p, name: e.target.value }))} />
                </div>
                <div className="quick-add-row">
                  <div className="field">
                    <label>SKU</label>
                    <input value={quickAddForm.sku} onChange={(e) => setQuickAddForm((p) => ({ ...p, sku: e.target.value }))} />
                  </div>
                  <div className="field">
                    <label>Base Unit</label>
                    <select value={quickAddForm.defaultUnit} onChange={(e) => setQuickAddForm((p) => ({ ...p, defaultUnit: e.target.value }))}>
                      {STANDARD_UNITS.map((u) => <option key={u} value={u}>{u}</option>)}
                    </select>
                  </div>
                </div>
                <div className="quick-add-row">
                  <div className="field">
                    <label>Category</label>
                    <select value={quickAddForm.categoryCode} onChange={(e) => setQuickAddForm((p) => ({ ...p, categoryCode: e.target.value }))}>
                      <option value="">-- optional --</option>
                      {categories.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label>Min Threshold</label>
                    <input type="number" value={quickAddForm.minThreshold} onChange={(e) => setQuickAddForm((p) => ({ ...p, minThreshold: e.target.value }))} />
                  </div>
                </div>
              </div>
              <div className="quick-add-footer">
                <button className="btn-secondary" onClick={() => setQuickAdd(null)}>Cancel</button>
                <button className="btn-primary" disabled={quickAddBusy || !quickAddForm.name || !quickAddForm.sku} onClick={submitQuickAdd}>
                  {quickAddBusy ? 'Creating...' : 'Create & Map'}
                </button>
              </div>
            </div>
          </div>
        )}
      </section>
    )
  }

  // Bill History (default)
  return (
    <section className="panel">
      {error && <div className="banner error">{error}</div>}
      {toast && <div className={`app-toast ${toast.type}`}>{toast.message}</div>}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <h3 style={{ margin: 0 }}>{showDeleted ? 'Deleted Bills' : 'Bills'}</h3>
        <div style={{ display: 'flex', gap: 8 }}>
          {!showDeleted && (
            <>
              <button className="btn-primary" onClick={() => { setError(''); history.pushState({ billsView: 'upload' }, ''); setView('upload') }}>
                Upload Hyperpure PDF
              </button>
              <button className="btn-secondary" onClick={() => { setError(''); openManualEntry() }}>
                + Add Manual Bill
              </button>
            </>
          )}
          <button className="btn-secondary" onClick={toggleDeletedView}>
            {showDeleted ? 'Back to Active Bills' : 'Show Deleted Bills'}
          </button>
        </div>
      </div>

      {/* Filters bar */}
      <div className="filters-bar">
        <div className="field" style={{ minWidth: 130 }}>
          <label>From</label>
          <input type="date" value={filterDateFrom} onChange={(e) => setFilterDateFrom(e.target.value)} />
        </div>
        <div className="field" style={{ minWidth: 130 }}>
          <label>To</label>
          <input type="date" value={filterDateTo} onChange={(e) => setFilterDateTo(e.target.value)} />
        </div>
        <div className="field" style={{ minWidth: 100 }}>
          <label>Source</label>
          <select value={filterSource} onChange={(e) => setFilterSource(e.target.value)}>
            <option value="all">All</option>
            <option value="hyperpure">Hyperpure</option>
            <option value="generic">Generic</option>
          </select>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6 }}>
          <button className="btn-small btn-primary" onClick={applyFilters}>Filter</button>
          <button className="btn-small btn-secondary" onClick={clearFilters}>Clear</button>
        </div>
      </div>

      {loading ? (
        <div className="skeleton-list">
          {[1, 2, 3].map((i) => (
            <div key={i} className="skeleton-row">
              <div className="skeleton-cell" style={{ width: '15%' }} />
              <div className="skeleton-cell" style={{ width: '20%' }} />
              <div className="skeleton-cell" style={{ width: '15%' }} />
              <div className="skeleton-cell" style={{ width: '8%' }} />
              <div className="skeleton-cell" style={{ width: '8%' }} />
              <div className="skeleton-cell" style={{ width: '12%' }} />
            </div>
          ))}
        </div>
      ) : bills.length === 0 ? (
        <div className="empty-state">
          {showDeleted
            ? 'No deleted bills.'
            : 'No bills uploaded yet. Upload a Hyperpure challan or enter a bill manually to get started.'}
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th><th>Bill # / Order #</th><th>Supplier</th><th>Source</th>
                <th>Items</th><th>Total ({'\u20B9'})</th>
                {showDeleted && <th>Deleted</th>}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {bills.map((bill) => (
                <Fragment key={bill.id}>
                  <tr>
                    <td>{bill.billDate ? new Date(bill.billDate).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) : '\u2013'}</td>
                    <td>{bill.orderNo || bill.billNumber || '\u2013'}</td>
                    <td>{bill.supplierName}</td>
                    <td><span className={`badge-${bill.source === 'hyperpure' ? 'info' : 'default'}`}>{bill.source === 'hyperpure' ? 'HP' : 'Manual'}</span></td>
                    <td>{bill.itemCount} items</td>
                    <td>{'\u20B9'}{bill.grandTotal?.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</td>
                    {showDeleted && (
                      <td>
                        {bill.deletedAt ? new Date(bill.deletedAt).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) : '\u2013'}
                        {bill.deletedByName ? <div style={{ fontSize: 12, color: 'var(--muted)' }}>{bill.deletedByName}</div> : null}
                      </td>
                    )}
                    <td>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button className="btn-small btn-secondary" onClick={() => toggleBillDetail(bill.id)}>
                          {expandedBillId === bill.id ? 'Hide' : 'View'}
                        </button>
                        {!showDeleted && (
                          <button className="btn-small btn-secondary" onClick={() => startEditBill(bill.id)}>
                            Edit
                          </button>
                        )}
                        {bill.hasFile && (
                          <button className="btn-small btn-secondary" onClick={() => downloadBill(bill.id)}>
                            Download
                          </button>
                        )}
                        {!showDeleted && (
                          <button className="btn-small btn-secondary" style={{ color: '#dc2626' }} onClick={() => setDeleteTarget(bill)}>
                            Delete
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                  {expandedBillId === bill.id && billDetail && (
                    <tr>
                      <td colSpan={showDeleted ? 8 : 7} style={{ padding: 16, background: '#f8fafc' }}>
                        <div className="table-wrap">
                          <table>
                            <thead>
                              <tr>
                                <th>#</th><th>Description</th><th>Qty</th><th>Unit Price</th>
                                <th>UoM</th><th>Total</th><th>Mapped To</th>
                              </tr>
                            </thead>
                            <tbody>
                              {billDetail.lineItems?.map((li, i) => (
                                <tr key={i}>
                                  <td>{li.slNo}</td>
                                  <td>{li.description}</td>
                                  <td>{li.quantity}</td>
                                  <td>{li.unitPrice}</td>
                                  <td>{li.uom}</td>
                                  <td>{'\u20B9'}{li.total}</td>
                                  <td>{li.inventoryItemName || '\u2013'} ({li.inventoryItemSku || ''})</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Delete confirmation */}
      {deleteTarget && (
        <div className="quick-add-overlay" onClick={() => { if (!deleting) setDeleteTarget(null) }}>
          <div className="quick-add-modal" onClick={(e) => e.stopPropagation()}>
            <div className="quick-add-header">
              <h4 style={{ margin: 0 }}>Delete Bill?</h4>
              <button className="searchable-select-clear" onClick={() => { if (!deleting) setDeleteTarget(null) }} style={{ fontSize: 16 }}>&#10005;</button>
            </div>
            <div className="quick-add-body">
              <p style={{ margin: 0 }}>
                Are you sure you want to delete this bill? It will be moved to Deleted Bills and
                removed from food cost calculations.
              </p>
              <div className="banner warning">
                <strong>{deleteTarget.supplierName || 'Unknown supplier'}</strong>
                {' \u2014 '}
                {deleteTarget.orderNo || deleteTarget.billNumber || 'No bill number'}
                {' \u2014 '}
                {'\u20B9'}{deleteTarget.grandTotal?.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                {' \u2014 '}
                {deleteTarget.itemCount} items
              </div>
            </div>
            <div className="quick-add-footer">
              <button className="btn-secondary" disabled={deleting} onClick={() => setDeleteTarget(null)}>Cancel</button>
              <button className="btn-primary" style={{ background: '#dc2626', borderColor: '#dc2626' }} disabled={deleting} onClick={confirmDeleteBill}>
                {deleting ? 'Deleting...' : 'Yes, Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
