const sql = require("mssql");
const { getPool } = require("../db");

const Equipment_Tool_PC =
  process.env.Equipment_Tool_PC || "[MasterKey].[dbo].[Equipment_Tool_PC]";

const Equipment_Tool_PC_LendingHistory =
  process.env.Equipment_Tool_PC_LendingHistory ||
  "[MasterKey].[dbo].[Equipment_Tool_PC_LendingHistory]";

/**
 * Thêm lịch sử mượn thiết bị
 */
const insertLendingHistory = async (req, res) => {
  try {
    const pool = await getPool();

    const {
      controlNo,
      Category,
      equipmentName,
      borrower,
      Borrow_Name,
      Code_Solenoid,
      borrowDate,
      Dept,
      Location,
    } = req.body;

    if (!controlNo || !borrower || !Code_Solenoid || !borrowDate) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Thiếu thông tin bắt buộc",
      });
    }

    const query = `
      INSERT INTO ${Equipment_Tool_PC_LendingHistory}
      (
        controlNo,
        Category,
        equipmentName,
        borrower,
        Borrow_Name,
        Code_Solenoid,
        borrowDate,
        Dept,
        Cabinet
      )
      VALUES
      (
        @controlNo,
        @Category,
        @equipmentName,
        @borrower,
        @Borrow_Name,
        @Code_Solenoid,
        @borrowDate,
        @Dept,
        @Location
      )
    `;

    const result = await pool
      .request()
      .input("controlNo", sql.VarChar, controlNo)
      .input("Category", sql.VarChar, Category)
      .input("equipmentName", sql.VarChar, equipmentName)
      .input("borrower", sql.VarChar, borrower)
      .input("Borrow_Name", sql.VarChar, Borrow_Name)
      .input("Code_Solenoid", sql.VarChar, Code_Solenoid)
      .input("borrowDate", borrowDate)
      .input("Dept", sql.VarChar, Dept)
      .input("Location", sql.VarChar, Location)
      .query(query);

    if (result.rowsAffected[0] === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không thể thêm lịch sử mượn thiết bị",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      message: "Thêm lịch sử mượn thiết bị thành công",
      rowsAffected: result.rowsAffected[0],
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
 * Trả thiết bị
 */
const updateStatusReturned = async (req, res) => {
  const pool = await getPool();
  const transaction = new sql.Transaction(pool);

  try {
    const { Code, Code_Solenoid, Dept, RFID_codes, controlNo, time } = req.body;

    if (!Code || !Code_Solenoid || !RFID_codes || !controlNo) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Thiếu dữ liệu đầu vào",
      });
    }

    await transaction.begin();

    const request = new sql.Request(transaction);

    request
      .input("Code", sql.VarChar, Code)
      .input("Dept", sql.VarChar, Dept)
      .input("RFID_codes", sql.VarChar, RFID_codes)
      .input("controlNo", sql.VarChar, controlNo)
      .input("Code_Solenoid", sql.VarChar, Code_Solenoid)
      .input("time", sql.DateTime, time);

    const query = `
      UPDATE ${Equipment_Tool_PC}
      SET
          RETURN_Requester = @Code,
          RETURN_requestDept = @Dept,
          RETURN_requestDate = @time,
          RETURN_verifyDate = @time,
          Using_Status = 'Returned'
      WHERE RFID_codes LIKE '%' + @RFID_codes + '%'
        AND controlNo = @controlNo;

      UPDATE ${Equipment_Tool_PC_LendingHistory}
      SET
          returnStatus = 'Returned',
          returnDate = @time,
          returnVerifier = @Code,
          verifyReturnDate = @time
      WHERE controlNo = @controlNo
        AND Code_Solenoid = @Code_Solenoid
        AND returnStatus IS NULL;
    `;

    const result = await request.query(query);

    await transaction.commit();

    const totalAffected = result.rowsAffected?.reduce((a, b) => a + b, 0) || 0;

    if (totalAffected === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy thiết bị hoặc lịch sử mượn cần cập nhật",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      message: "Cập nhật trạng thái trả thiết bị thành công",
      rowsAffected: totalAffected,
    });
  } catch (error) {
    await transaction.rollback().catch(() => {});

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
  insertLendingHistory,
  updateStatusReturned,
};
