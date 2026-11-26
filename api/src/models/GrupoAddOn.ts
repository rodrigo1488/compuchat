import {
  Table,
  Column,
  CreatedAt,
  UpdatedAt,
  Model,
  DataType,
  PrimaryKey,
  AutoIncrement,
  ForeignKey,
  BelongsTo,
  AfterBulkDestroy,
  AfterBulkUpdate,
  AfterCreate,
  AfterDestroy,
  AfterUpdate,
} from "sequelize-typescript";
import Company from "./Company";
import AddOnGroup from "./AddOnGroup";
import {
  invalidateCatalogBulk,
  invalidateCatalogInstance
} from "../helpers/companyCacheHooks";

@Table({
  indexes: [
    {
      unique: true,
      fields: ["companyId", "grupo"],
    },
  ],
})
class GrupoAddOn extends Model<GrupoAddOn> {
  @PrimaryKey
  @AutoIncrement
  @Column
  id: number;

  @ForeignKey(() => Company)
  @Column
  companyId: number;

  @BelongsTo(() => Company)
  company: Company;

  @Column
  grupo: string;

  @ForeignKey(() => AddOnGroup)
  @Column
  addOnGroupId: number;

  @BelongsTo(() => AddOnGroup)
  addOnGroup: AddOnGroup;

  @CreatedAt
  createdAt: Date;

  @UpdatedAt
  updatedAt: Date;

  @AfterCreate
  @AfterUpdate
  @AfterDestroy
  static invalidateCatalog(instance: GrupoAddOn): void {
    invalidateCatalogInstance(instance);
  }

  @AfterBulkUpdate
  @AfterBulkDestroy
  static invalidateCatalogBulk(options: { where?: { companyId?: unknown } }): void {
    invalidateCatalogBulk(options);
  }
}

export default GrupoAddOn;
