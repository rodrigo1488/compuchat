import {
  Table,
  Column,
  CreatedAt,
  UpdatedAt,
  Model,
  DataType,
  PrimaryKey,
  AutoIncrement,
  Default,
  ForeignKey,
  BelongsTo,
  AfterBulkDestroy,
  AfterBulkUpdate,
  AfterCreate,
  AfterDestroy,
  AfterUpdate,
} from "sequelize-typescript";
import Company from "./Company";
import Contact from "./Contact";
import Ticket from "./Ticket";
import Form from "./Form";
import {
  invalidateCatalogBulk,
  invalidateCatalogInstance
} from "../helpers/companyCacheHooks";

@Table
class Mesa extends Model<Mesa> {
  @PrimaryKey
  @AutoIncrement
  @Column
  id: number;

  @Column
  number: string;

  @Column
  name: string;

  @Default("livre")
  @Column
  status: string;

  @Default("mesa")
  @Column
  type: string;

  @ForeignKey(() => Company)
  @Column
  companyId: number;

  @BelongsTo(() => Company)
  company: Company;

  @ForeignKey(() => Contact)
  @Column
  contactId: number;

  @BelongsTo(() => Contact)
  contact: Contact;

  @ForeignKey(() => Ticket)
  @Column
  ticketId: number;

  @BelongsTo(() => Ticket)
  ticket: Ticket;

  @ForeignKey(() => Form)
  @Column
  formId: number | null;

  @BelongsTo(() => Form)
  form: Form;

  @Column
  capacity: number;

  @Column
  section: string;

  @Column
  displayOrder: number;

  @Column
  occupiedAt: Date;

  @Column
  sessionId: string;

  @CreatedAt
  createdAt: Date;

  @UpdatedAt
  updatedAt: Date;

  @AfterCreate
  @AfterUpdate
  @AfterDestroy
  static invalidateCatalog(instance: Mesa): void {
    invalidateCatalogInstance(instance);
  }

  @AfterBulkUpdate
  @AfterBulkDestroy
  static invalidateCatalogBulk(options: { where?: { companyId?: unknown } }): void {
    invalidateCatalogBulk(options);
  }
}

export default Mesa;
