const sql = require("mssql");
const { getPool } = require("../db");

const User_Info =
  process.env.User_Info || "[UserInformation].[dbo].[User_Info]";

const Code_Solenoid_Checker =
  process.env.Code_Solenoid_Checker ||
  "[MasterKey].[dbo].[Code_Solenoid_Checker]";

/**
 * Lấy thông tin user theo Code_Solenoid
 */
const getUser = async (req, res) => {
  try {
    const pool = await getPool();

    const { Code_Solenoid } = req.query;
    const codeSolenoid = Code_Solenoid?.trim();

    if (!codeSolenoid) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Code_Solenoid không được để trống",
      });
    }

    const query = `
       SELECT
    C.id,
    ISNULL(C.Code_Solenoid, U.Code_Solenoid) AS Code_Solenoid,
    C.[DATETIME],
    ISNULL(C.FullName, U.FullName) AS FullName,
    ISNULL(C.Code, U.Code) AS Code,
    U.Email,
    U.Dept
FROM ${User_Info} U
FULL OUTER JOIN ${Code_Solenoid_Checker} C
    ON C.Code = U.Code 
    OR TRIM(C.Code_Solenoid) = TRIM(U.Code_Solenoid)
WHERE TRIM(U.Code_Solenoid) = TRIM(@codeSolenoid)
   OR TRIM(C.Code_Solenoid) = TRIM(@codeSolenoid);

    `;

    const result = await pool
      .request()
      .input("codeSolenoid", sql.VarChar, codeSolenoid)
      .query(query);

    if (!result.recordset.length) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy người dùng",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      data: result.recordset[0],
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

/**
 * Thêm mới hoặc cập nhật Code_Solenoid
 */
const insertNewCommer = async (req, res) => {
  try {
    const pool = await getPool();

    const { Code, Code_Solenoid } = req.body;

    if (!Code || !Code_Solenoid) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Code và Code_Solenoid không được để trống",
      });
    }

    const query = `
        IF EXISTS
        (
            SELECT 1
            FROM ${Code_Solenoid_Checker}
            WHERE Code = @Code
        )
        BEGIN
            UPDATE ${Code_Solenoid_Checker}
            SET Code_Solenoid = @Code_Solenoid
            WHERE Code = @Code
        END
        ELSE
        BEGIN
            INSERT INTO ${Code_Solenoid_Checker}
            (
                Code,
                Code_Solenoid
            )
            VALUES
            (
                @Code,
                @Code_Solenoid
            )
        END
    `;

    const result = await pool
      .request()
      .input("Code", sql.VarChar, Code)
      .input("Code_Solenoid", sql.VarChar, Code_Solenoid)
      .query(query);

    const affected = result.rowsAffected?.reduce((a, b) => a + b, 0) || 0;

    return res.json({
      code: 200,
      result: "OK",
      message: affected > 0 ? "Cập nhật thành công" : "Thực hiện thành công",
      rowsAffected: affected,
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

module.exports = {
  getUser,
  insertNewCommer,
};
