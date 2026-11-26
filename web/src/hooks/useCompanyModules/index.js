import { useContext } from "react";
import { useQuery } from "react-query";
import api from "../../services/api";
import { queryClient } from "../../services/queryClient";
import { AuthContext } from "../../context/Auth/AuthContext";

const MODULE_LANCHONETES = "lanchonetes";
const MODULE_AGENDAMENTO = "agendamento";

export const invalidateCompanyModulesCache = (companyId) => {
  if (companyId != null) {
    queryClient.invalidateQueries(["company-modules", companyId]);
  } else {
    queryClient.invalidateQueries(["company-modules"]);
  }
};

const useCompanyModules = () => {
  const { user, isAuth } = useContext(AuthContext);
  const companyId = user?.companyId;
  const enabled = Boolean(isAuth && companyId != null);

  const { data, isLoading, refetch } = useQuery(
    ["company-modules", companyId],
    async () => {
      const res = await api.get("/company/modules");
      return res.data?.modules || [];
    },
    {
      enabled,
      staleTime: 5 * 60 * 1000,
      cacheTime: 30 * 60 * 1000,
      retry: false,
    }
  );

  const modules = data || [];
  const hasModule = (moduleName) => modules.includes(moduleName);

  return {
    modules,
    loading: enabled && isLoading,
    hasModule,
    hasLanchonetes: hasModule(MODULE_LANCHONETES),
    hasAgendamento: hasModule(MODULE_AGENDAMENTO),
    refetch: () => refetch(),
  };
};

export default useCompanyModules;
export { MODULE_LANCHONETES, MODULE_AGENDAMENTO };
