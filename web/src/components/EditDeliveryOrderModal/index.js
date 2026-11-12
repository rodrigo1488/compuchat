import React, { useEffect, useMemo, useState } from "react";
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  TextField,
  Typography,
} from "@material-ui/core";
import MenuProductPicker from "../MenuProductPicker";
import { filterMenuProducts, filterProductsByMenuCatalog, formatMoney } from "../../utils/menuProductHelpers";
import { Add as AddIcon, Delete as DeleteIcon, Remove as RemoveIcon } from "@material-ui/icons";
import api from "../../services/api";
import toastError from "../../errors/toastError";
import { toast } from "react-toastify";

const cloneItems = (items) =>
  (items || []).map((it) => ({
    ...it,
    quantity: Number(it.quantity) || 1,
    addons: Array.isArray(it.addons) ? [...it.addons] : [],
  }));

const answerText = (answer) => {
  if (answer == null) return "";
  if (Array.isArray(answer)) return answer.map(String).filter(Boolean).join(", ").trim();
  return String(answer).trim();
};

const findAnswerValue = (answers, predicate) => {
  for (const row of answers || []) {
    const field = row?.field;
    if (!field || !predicate(field)) continue;
    const value = answerText(row.answer);
    if (value) return value;
  }
  return "";
};

const readDeliveryAddress = (answers, meta) => {
  const byAuto = (type) =>
    findAnswerValue(
      answers,
      (field) => String(field.metadata?.autoFieldType || "") === type
    );
  const byLabel = (patterns) =>
    findAnswerValue(answers, (field) => {
      const label = String(field.label || "").toLowerCase();
      return patterns.some((pattern) => pattern.test(label));
    });
  const source = meta || {};
  return {
    endereco:
      byAuto("address") ||
      byLabel([/^endereco$/, /endereço/, /rua/]) ||
      source.endereco ||
      "",
    endereconumero:
      byAuto("number") ||
      byLabel([/n[uú]mero/, /^numero$/]) ||
      source.endereconumero ||
      "",
    enderecobairro:
      byAuto("neighborhood") ||
      byLabel([/bairro/]) ||
      source.enderecobairro ||
      "",
    enderecocomplemento:
      byAuto("complement") ||
      byLabel([/complemento/]) ||
      source.enderecocomplemento ||
      "",
    enderecoreferencia:
      byAuto("reference") ||
      byLabel([/refer[eê]ncia/, /referencia/]) ||
      source.enderecoreferencia ||
      "",
  };
};

const EditDeliveryOrderModal = ({ open, order, onClose, onSaved }) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [products, setProducts] = useState([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [menuItems, setMenuItems] = useState([]);
  const [responderName, setResponderName] = useState("");
  const [mode, setMode] = useState("delivery");
  const [configuredFee, setConfiguredFee] = useState(0);
  const [feeWaived, setFeeWaived] = useState(false);
  const [address, setAddress] = useState({
    endereco: "",
    endereconumero: "",
    enderecobairro: "",
    enderecocomplemento: "",
    enderecoreferencia: "",
  });

  const formId = order?.formId || order?.form?.id;
  const orderId = order?.id;
  const isPickup = mode === "pickup";

  useEffect(() => {
    if (!open || !orderId || !formId) return undefined;

    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const [{ data: responseData }, { data: formData }] = await Promise.all([
          api.get(`/forms/${formId}/responses/${orderId}`),
          api.get(`/forms/${formId}`),
        ]);
        if (cancelled) return;
        const meta = responseData.metadata || order?.metadata || {};
        const fromResponse = readDeliveryAddress(responseData.answers, meta);
        const fromOrder = readDeliveryAddress(order?.answers, order?.metadata);
        setMenuItems(cloneItems(meta.menuItems || []));
        setResponderName(responseData.responderName || meta.customerName || "");
        const pickup =
          meta.fulfillmentMode === "pickup" || meta.pickup === true || meta.retirada === true;
        setMode(pickup ? "pickup" : "delivery");
        setFeeWaived(!pickup && meta.deliveryFeeWaived === true);
        setConfiguredFee(Number(formData.settings?.deliveryFee) || 0);
        setAddress({
          endereco: fromResponse.endereco || fromOrder.endereco || "",
          endereconumero: fromResponse.endereconumero || fromOrder.endereconumero || "",
          enderecobairro: fromResponse.enderecobairro || fromOrder.enderecobairro || "",
          enderecocomplemento:
            fromResponse.enderecocomplemento || fromOrder.enderecocomplemento || "",
          enderecoreferencia:
            fromResponse.enderecoreferencia || fromOrder.enderecoreferencia || "",
        });

        const publicId = formData.publicId;
        if (publicId) {
          const { data: prodData } = await api.get(`/public/forms/${publicId}/products`);
          if (cancelled) return;
          setProducts(
            filterMenuProducts(
              filterProductsByMenuCatalog(prodData.products || [], formData.settings)
            )
          );
        } else {
          setProducts([]);
        }
      } catch (err) {
        if (cancelled) return;
        toastError(err);
        onClose();
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
    // Recarrega só ao abrir o pedido. `order` e `onClose` mudam a cada
    // atualização da lista e apagavam o endereço no meio da edição.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, orderId, formId]);

  const storedFee = Number(order?.metadata?.deliveryFee) || 0;
  const startedAsPickup =
    order?.metadata?.fulfillmentMode === "pickup" ||
    order?.metadata?.pickup === true ||
    order?.metadata?.retirada === true;
  const deliveryFee = isPickup || feeWaived
    ? 0
    : startedAsPickup
      ? (configuredFee || storedFee)
      : (storedFee || configuredFee);
  const canWaiveFee = !isPickup && (deliveryFee > 0 || feeWaived || configuredFee > 0 || storedFee > 0);
  const couponDiscount = Number(order?.metadata?.couponDiscount) || 0;

  const subtotal = useMemo(
    () =>
      menuItems.reduce((sum, it) => {
        const qty = Number(it.quantity) || 0;
        const unit = (Number(it.productValue) || 0) + (Number(it.addonsTotal) || 0);
        return sum + qty * unit;
      }, 0),
    [menuItems]
  );

  const total = Math.max(0, subtotal + deliveryFee - couponDiscount);

  const updateQty = (index, delta) => {
    setMenuItems((prev) =>
      prev
        .map((it, i) => {
          if (i !== index) return it;
          const nextQty = Math.max(0, (Number(it.quantity) || 1) + delta);
          return { ...it, quantity: nextQty };
        })
        .filter((it) => Number(it.quantity) > 0)
    );
  };

  const removeItem = (index) => {
    setMenuItems((prev) => prev.filter((_, i) => i !== index));
  };

  const appendMenuItem = (item) => {
    if (!item) return;
    setMenuItems((prev) => [...prev, item]);
    toast.success("Produto adicionado ao pedido.");
  };

  const handleSave = async () => {
    if (!menuItems.length) {
      toast.error("Adicione ao menos um item ao pedido.");
      return;
    }
    if (!isPickup && !String(address.endereco || "").trim()) {
      toast.error("Informe o endereço para pedidos com entrega.");
      return;
    }

    setSaving(true);
    try {
      const payload = {
        responderName: responderName.trim(),
        fulfillmentMode: mode,
        waiveDeliveryFee: !isPickup && feeWaived,
        menuItems: menuItems.map((it) => ({
          ...it,
          quantity: Number(it.quantity) || 1,
        })),
        address: isPickup
          ? undefined
          : {
              endereco: address.endereco.trim(),
              endereconumero: address.endereconumero.trim(),
              enderecobairro: address.enderecobairro.trim(),
              enderecocomplemento: address.enderecocomplemento.trim(),
              enderecoreferencia: address.enderecoreferencia.trim(),
            },
      };
      const { data } = await api.put(
        `/forms/${formId}/responses/${orderId}/delivery-order`,
        payload
      );
      toast.success(data?.message || "Pedido atualizado.");
      if (onSaved) onSaved(data.response);
      onClose();
    } catch (err) {
      toastError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        Editar pedido delivery
        {order?.protocol ? ` — ${order.protocol}` : ""}
      </DialogTitle>
      <DialogContent dividers>
        {loading ? (
          <Box display="flex" justifyContent="center" py={4}>
            <CircularProgress />
          </Box>
        ) : (
          <>
            <TextField
              label="Nome do cliente"
              value={responderName}
              onChange={(e) => setResponderName(e.target.value)}
              fullWidth
              margin="dense"
              variant="outlined"
            />

            <Box display="flex" mt={2} mb={1}>
              <Button
                variant={mode === "delivery" ? "contained" : "outlined"}
                color="primary"
                onClick={() => setMode("delivery")}
                style={{ marginRight: 8 }}
              >
                Delivery
              </Button>
              <Button
                variant={mode === "pickup" ? "contained" : "outlined"}
                color="primary"
                onClick={() => {
                  setMode("pickup");
                  setFeeWaived(false);
                }}
              >
                Retirada no balcão
              </Button>
            </Box>

            {!isPickup && (
              <Box mt={2}>
                <Typography variant="subtitle2" gutterBottom>
                  Endereço
                </Typography>
                <TextField
                  label="Logradouro"
                  value={address.endereco}
                  onChange={(e) => setAddress((a) => ({ ...a, endereco: e.target.value }))}
                  fullWidth
                  margin="dense"
                  variant="outlined"
                />
                <Box display="flex" gridGap={8}>
                  <TextField
                    label="Número"
                    value={address.endereconumero}
                    onChange={(e) =>
                      setAddress((a) => ({ ...a, endereconumero: e.target.value }))
                    }
                    margin="dense"
                    variant="outlined"
                    style={{ flex: 1 }}
                  />
                  <TextField
                    label="Bairro"
                    value={address.enderecobairro}
                    onChange={(e) =>
                      setAddress((a) => ({ ...a, enderecobairro: e.target.value }))
                    }
                    margin="dense"
                    variant="outlined"
                    style={{ flex: 2 }}
                  />
                </Box>
                <TextField
                  label="Complemento"
                  value={address.enderecocomplemento}
                  onChange={(e) =>
                    setAddress((a) => ({ ...a, enderecocomplemento: e.target.value }))
                  }
                  fullWidth
                  margin="dense"
                  variant="outlined"
                />
                <TextField
                  label="Referência"
                  value={address.enderecoreferencia}
                  onChange={(e) =>
                    setAddress((a) => ({ ...a, enderecoreferencia: e.target.value }))
                  }
                  fullWidth
                  margin="dense"
                  variant="outlined"
                />
              </Box>
            )}

            {isPickup && (
              <Typography variant="body2" color="textSecondary" style={{ marginTop: 12 }}>
                Retirada no balcão — a taxa de entrega sai do pedido.
              </Typography>
            )}
            {!isPickup && startedAsPickup && !feeWaived && configuredFee > 0 && (
              <Typography variant="body2" color="textSecondary" style={{ marginTop: 8 }}>
                Taxa de entrega de R$ {configuredFee.toFixed(2).replace(".", ",")} será adicionada.
              </Typography>
            )}
            {canWaiveFee && (
              <Box mt={1}>
                <Button
                  size="small"
                  variant="outlined"
                  color="secondary"
                  onClick={() => setFeeWaived((current) => !current)}
                >
                  {feeWaived ? "Restaurar taxa de entrega" : "Remover taxa de entrega"}
                </Button>
                {feeWaived && (
                  <Typography variant="body2" color="textSecondary" style={{ marginTop: 8 }}>
                    Taxa de entrega removida. O pedido continua como delivery.
                  </Typography>
                )}
              </Box>
            )}

            <Divider style={{ margin: "16px 0" }} />

            <Typography variant="subtitle2" gutterBottom>
              Itens do pedido
            </Typography>
            {menuItems.map((it, index) => {
              const unit = (Number(it.productValue) || 0) + (Number(it.addonsTotal) || 0);
              const line = unit * (Number(it.quantity) || 1);
              return (
                <Box
                  key={`${it.productId}-${index}-${it.productName}`}
                  display="flex"
                  alignItems="center"
                  justifyContent="space-between"
                  py={0.5}
                >
                  <Box flex={1} pr={1}>
                    <Typography variant="body2">
                      {it.productName || `Produto #${it.productId}`}
                    </Typography>
                    {(it.addons || []).length > 0 && (
                      <Typography variant="caption" color="textSecondary" display="block">
                        + {(it.addons || []).map((a) => a.label).join(", ")}
                      </Typography>
                    )}
                    {it.observation && (
                      <Typography variant="caption" color="textSecondary" display="block">
                        Obs: {it.observation}
                      </Typography>
                    )}
                    <Typography variant="caption" color="textSecondary">
                      {formatMoney(line)}
                    </Typography>
                  </Box>
                  <Box display="flex" alignItems="center">
                    <IconButton size="small" onClick={() => updateQty(index, -1)}>
                      <RemoveIcon fontSize="small" />
                    </IconButton>
                    <Typography variant="body2" style={{ minWidth: 24, textAlign: "center" }}>
                      {it.quantity}
                    </Typography>
                    <IconButton size="small" onClick={() => updateQty(index, 1)}>
                      <AddIcon fontSize="small" />
                    </IconButton>
                    <IconButton size="small" onClick={() => removeItem(index)}>
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Box>
                </Box>
              );
            })}

            <Box mt={2}>
              <Button
                variant="outlined"
                color="primary"
                fullWidth
                onClick={() => setPickerOpen(true)}
                disabled={!products.length}
              >
                Adicionar produto do cardápio
              </Button>
            </Box>

            <Box mt={2} display="flex" justifyContent="space-between">
              <Typography variant="body2" color="textSecondary">
                Subtotal: R$ {subtotal.toFixed(2).replace(".", ",")}
                {deliveryFee > 0
                  ? ` · Taxa: R$ ${deliveryFee.toFixed(2).replace(".", ",")}`
                  : ""}
              </Typography>
              <Typography variant="subtitle1" color="primary">
                Total: R$ {total.toFixed(2).replace(".", ",")}
              </Typography>
            </Box>
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>
          Cancelar
        </Button>
        <Button
          color="primary"
          variant="contained"
          onClick={handleSave}
          disabled={loading || saving || !menuItems.length}
        >
          {saving ? "Salvando..." : "Salvar e reimprimir"}
        </Button>
      </DialogActions>
      <MenuProductPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        products={products}
        onAddItem={appendMenuItem}
        title="Cardápio — adicionar ao pedido"
      />
    </Dialog>
  );
};

export default EditDeliveryOrderModal;
