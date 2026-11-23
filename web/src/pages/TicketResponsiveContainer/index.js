import React, { useContext, useEffect, useState } from "react";
import { useHistory } from "react-router-dom";
import withWidth, { isWidthUp } from "@material-ui/core/withWidth";

import Tickets from "../TicketsCustom";
import TicketAdvanced from "../TicketsAdvanced";
import BackdropLoading from "../../components/BackdropLoading";
import api from "../../services/api";
import { AuthContext } from "../../context/Auth/AuthContext";
import toastError from "../../errors/toastError";

function phoneDigits(value) {
	return String(value || "").replace(/\D/g, "");
}

function samePhone(left, right) {
	const a = phoneDigits(left);
	const b = phoneDigits(right);
	if (!a || !b) return false;
	if (a === b) return true;
	if (a.startsWith("55") && a.slice(2) === b) return true;
	return b.startsWith("55") && b.slice(2) === a;
}

async function findContact(phone) {
	const { data } = await api.get("/contacts", { params: { searchParam: phone } });
	return (data.contacts || []).find((contact) => samePhone(contact.number, phone)) || null;
}

async function findOrCreateContact(phone, name) {
	const existing = await findContact(phone);
	if (existing) return existing;
	const local = phone.startsWith("55") && phone.length > 11 ? phone.slice(2) : "";
	if (local) {
		const byLocal = await findContact(local);
		if (byLocal) return byLocal;
	}
	try {
		const { data } = await api.post("/contacts", {
			name: name || phone,
			number: phone
		});
		return data;
	} catch (err) {
		const again = (await findContact(phone)) || (local ? await findContact(local) : null);
		if (again) return again;
		throw err;
	}
}

function TicketResponsiveContainer(props) {
	const history = useHistory();
	const { user } = useContext(AuthContext);
	const params = new URLSearchParams(history.location.search || "");
	const phone = phoneDigits(params.get("phone") || params.get("telefone"));
	const name = String(params.get("name") || params.get("nome") || "").trim();
	const [opening, setOpening] = useState(Boolean(phone));

	useEffect(() => {
		if (!phone || !user?.id) return undefined;
		let cancelled = false;
		(async () => {
			try {
				const contact = await findOrCreateContact(phone, name);
				const { data: ticket } = await api.post("/tickets", {
					contactId: contact.id,
					status: "open",
					userId: user.id,
					reuseOpenTicket: true
				});
				if (cancelled) return;
				history.replace(`/tickets/${ticket.uuid || ticket.id}`);
			} catch (err) {
				if (cancelled) return;
				toastError(err);
				history.replace("/tickets");
			} finally {
				if (!cancelled) setOpening(false);
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [phone, name, user, history]);

	if (phone && (opening || !user?.id)) {
		return <BackdropLoading />;
	}
	if (isWidthUp("md", props.width)) {
		return <Tickets />;
	}
	return <TicketAdvanced />;
}

export default withWidth()(TicketResponsiveContainer);
