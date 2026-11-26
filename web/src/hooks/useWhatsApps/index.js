import { useEffect, useContext } from "react";
import { useQuery } from "react-query";
import toastError from "../../errors/toastError";
import api from "../../services/api";
import { queryClient } from "../../services/queryClient";
import { SocketContext } from "../../context/Socket/SocketContext";

const EMPTY_WHATSAPPS = [];

const whatsAppKey = (companyId) => ["whatsapps", companyId || "none"];

const useWhatsApps = () => {
  const socketManager = useContext(SocketContext);
  const companyId = localStorage.getItem("companyId");

  const { data, isLoading } = useQuery(
    whatsAppKey(companyId),
    async () => {
      const { data: payload } = await api.get("/whatsapp/?session=0");
      return payload;
    },
    {
      enabled: Boolean(companyId),
      staleTime: 60 * 1000,
      retry: false,
      onError: (err) => toastError(err),
    }
  );

  useEffect(() => {
    if (!companyId) return undefined;
    const socket = socketManager.getSocket(companyId);

    const updateList = (updater) => {
      queryClient.setQueryData(whatsAppKey(companyId), (current) => {
        const list = Array.isArray(current) ? current : [];
        return updater(list);
      });
    };

    const handleWhatsApp = (payload) => {
      if (payload.action === "update") {
        updateList((list) => {
          const index = list.findIndex((item) => item.id === payload.whatsapp.id);
          if (index === -1) return [payload.whatsapp, ...list];
          const next = list.slice();
          next[index] = payload.whatsapp;
          return next;
        });
      }
      if (payload.action === "delete") {
        updateList((list) => list.filter((item) => item.id !== payload.whatsappId));
      }
    };

    const handleWhatsAppSession = (payload) => {
      if (payload.action !== "update") return;
      updateList((list) =>
        list.map((item) =>
          item.id === payload.session.id
            ? {
                ...item,
                status: payload.session.status,
                updatedAt: payload.session.updatedAt,
                qrcode: payload.session.qrcode,
                retries: payload.session.retries,
              }
            : item
        )
      );
    };

    socket.on(`company-${companyId}-whatsapp`, handleWhatsApp);
    socket.on(`company-${companyId}-whatsappSession`, handleWhatsAppSession);

    return () => {
      socket.off(`company-${companyId}-whatsapp`, handleWhatsApp);
      socket.off(`company-${companyId}-whatsappSession`, handleWhatsAppSession);
    };
  }, [socketManager, companyId]);

  return { whatsApps: data || EMPTY_WHATSAPPS, loading: isLoading };
};

export default useWhatsApps;
